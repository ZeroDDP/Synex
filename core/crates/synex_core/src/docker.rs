//! Docker Engine API через SSH-туннель к сокету демона.
//!
//! Docker живёт на удалённом сервере, порт наружу не открываем: воркер открывает
//! SSH-канал `direct-streamlocal@openssh.com` к `/var/run/docker.sock` и говорит по нему
//! обычный HTTP. bollard получает этот поток как «custom transport» — на каждый запрос
//! открывается свой канал, поэтому сессия занята только на время открытия, а не на всё
//! время выполнения (важно для `docker_logs` с follow).
//!
//! Ответы отдаются в форме backend (`backend/app/plugins/docker/schemas.py`): JSON в
//! `payload.stdout`, структура полей повторяет pydantic-модели, чтобы backend мог
//! переключиться на воркер без правок схем.

use std::collections::HashMap;
use std::future::Future;
use std::pin::Pin;
use std::sync::Arc;
use std::time::Duration;

use bollard::Docker;
use bollard::container::LogOutput;
use bollard::models::{
    ContainerStatsResponse, ContainerSummary, ImageSummary, SystemVersion,
};
use bollard::query_parameters::{
    ListContainersOptions, LogsOptions, RemoveContainerOptions, RestartContainerOptions,
    StopContainerOptions,
};
use chrono::{DateTime, NaiveDateTime, SecondsFormat, Utc};
use futures_util::StreamExt;
use serde::Serialize;
use tokio::sync::{Mutex, mpsc};
use tokio_util::sync::CancellationToken;

use crate::errors::{Result, SynexError};
use crate::ssh::SshSession;

// Путь сокета Docker по умолчанию на сервере.
pub const DEFAULT_SOCKET: &str = "/var/run/docker.sock";

// Коды ошибок: они стоят в начале stderr ответа.
pub const ERR_NOT_INSTALLED: &str = "docker_not_installed";
pub const ERR_PERMISSION: &str = "docker_permission_denied";
pub const ERR_DAEMON: &str = "docker_daemon_unavailable";
pub const ERR_NOT_FOUND: &str = "docker_container_not_found";
pub const ERR_CONFLICT: &str = "docker_container_conflict";
pub const ERR_TIMEOUT: &str = "docker_timeout";
pub const ERR_FAILED: &str = "docker_command_failed";

// Общий лимит на обычный запрос и на отдельные случаи.
const REQUEST_TIMEOUT: Duration = Duration::from_secs(60);
const INFO_TIMEOUT: Duration = Duration::from_secs(10);
const STATS_TIMEOUT: Duration = Duration::from_secs(8);
// Клиент bollard: таймаут на уровень соединения, секунды.
const CLIENT_TIMEOUT_SECS: u64 = 30;

// Тексты демона, означающие «действие не подходит к текущему состоянию».
const CONFLICT_MARKERS: [&str; 6] = [
    "is not running",
    "is already paused",
    "is not paused",
    "container is running",
    "is restarting",
    "already started",
];

// Ключи compose-меток.
const COMPOSE_PROJECT: &str = "com.docker.compose.project";
const COMPOSE_SERVICE: &str = "com.docker.compose.service";

/// Сессия SSH в том виде, в котором её держит воркер.
pub type SessionHandle = Arc<Mutex<SshSession>>;

// ===== Модели ответа (форма backend) =====

#[derive(Serialize, Debug, Clone)]
pub struct DockerInfo {
    // ok | not_installed | permission_denied | daemon_unavailable
    pub status: &'static str,
    pub detail: Option<String>,
    pub client_version: Option<String>,
    pub server_version: Option<String>,
    pub api_version: Option<String>,
    pub os: Option<String>,
    pub arch: Option<String>,
}

#[derive(Serialize, Debug, Clone, PartialEq)]
pub struct PortInfo {
    pub container_port: String,
    pub protocol: String,
    pub host_ip: Option<String>,
    pub host_port: Option<String>,
}

#[derive(Serialize, Debug, Clone)]
pub struct ContainerInfo {
    pub id: String,
    pub name: String,
    pub names: Vec<String>,
    pub image: String,
    pub command: String,
    pub created_at: Option<String>,
    pub state: String,
    pub status: String,
    pub health: Option<String>,
    pub exit_code: Option<i32>,
    pub ports: Vec<PortInfo>,
    pub labels: HashMap<String, String>,
    pub networks: Vec<String>,
    pub compose_project: Option<String>,
    pub compose_service: Option<String>,
}

#[derive(Serialize, Debug, Clone)]
pub struct LogLineInfo {
    pub time: String,
    // stdout | stderr
    pub stream: &'static str,
    pub text: String,
}

#[derive(Serialize, Debug, Clone)]
pub struct LogsBatch {
    pub lines: Vec<LogLineInfo>,
    pub next_since: Option<String>,
}

#[derive(Serialize, Debug, Clone)]
pub struct ContainerStatsInfo {
    pub id: String,
    pub name: String,
    pub cpu_percent: Option<f64>,
    pub memory_used: Option<u64>,
    pub memory_limit: Option<u64>,
    pub memory_percent: Option<f64>,
    pub net_rx: Option<u64>,
    pub net_tx: Option<u64>,
    pub block_read: Option<u64>,
    pub block_write: Option<u64>,
    pub pids: Option<u64>,
}

#[derive(Serialize, Debug, Clone)]
pub struct ImageInfo {
    pub id: String,
    pub repository: Option<String>,
    pub tag: Option<String>,
    pub digest: Option<String>,
    pub created_at: Option<String>,
    pub size: Option<i64>,
}

// Результат действия над контейнером: состояние после действия плюс то, что печатает docker.
#[derive(Serialize, Debug, Clone)]
pub struct ContainerActionOutcome {
    pub container: ContainerInfo,
    pub action: String,
    pub output: String,
}

// Параметры запроса логов.
#[derive(Debug, Clone, Default)]
pub struct LogsRequest {
    pub container: String,
    pub tail: Option<String>,
    pub since: Option<String>,
    pub follow: bool,
}

// ===== Клиент =====

// Тип future, который ждёт от транспорта bollard: ответ hyper на BolalrdRequest.
type DockerFuture = Pin<
    Box<
        dyn Future<
                Output = std::result::Result<
                    hyper::Response<hyper::body::Incoming>,
                    bollard::errors::Error,
                >,
            > + Send,
    >,
>;

// Ошибку вне bollard заворачиваем в DockerResponseServerError с нашим кодом 599:
// так транспорт возвращает ошибку, не завися от приватных вариантов bollard.
const TRANSPORT_STATUS: u16 = 599;

fn transport_error(message: String) -> bollard::errors::Error {
    bollard::errors::Error::DockerResponseServerError {
        status_code: TRANSPORT_STATUS,
        message,
    }
}

// Создаёт bollard-клиент. На каждый HTTP-запрос открывается новый SSH-канал к сокету:
// сессия занята только на время открытия канала.
fn make_client(session: SessionHandle, socket: &str) -> Result<Docker> {
    let socket = socket.to_string();

    let transport = move |req: bollard::BollardRequest| -> DockerFuture {
        let session = session.clone();
        let socket = socket.clone();

        Box::pin(async move {
            let stream = {
                let guard = session.lock().await;
                guard
                    .open_streamlocal_channel(&socket)
                    .await
                    .map_err(|err| {
                        transport_error(format!("cannot open docker socket channel: {}", err))
                    })?
                    .into_stream()
            };

            let io = hyper_util::rt::TokioIo::new(stream);
            let (mut sender, conn) = hyper::client::conn::http1::handshake(io)
                .await
                .map_err(|err| transport_error(format!("docker handshake failed: {}", err)))?;

            // Соединение живёт в своей задаче: оно дочитывает тело ответа,
            // пока bollard его разбирает.
            tokio::spawn(async move {
                let _ = conn.await;
            });

            sender
                .send_request(req)
                .await
                .map_err(|err| transport_error(format!("docker request failed: {}", err)))
        })
    };

    Docker::connect_with_custom_transport(
        transport,
        Some("http://docker"),
        CLIENT_TIMEOUT_SECS,
        bollard::API_DEFAULT_VERSION,
    )
    .map_err(|err| SynexError::Docker(ERR_DAEMON, err.to_string()))
}

// Переводит ошибку bollard в код docker_*.
fn map_docker_error(err: bollard::errors::Error) -> SynexError {
    if let bollard::errors::Error::DockerResponseServerError {
        status_code,
        message,
    } = &err
    {
        let lower = message.to_lowercase();
        let code = match *status_code {
            TRANSPORT_STATUS => ERR_DAEMON,
            403 => ERR_PERMISSION,
            404 => ERR_NOT_FOUND,
            304 | 409 => ERR_CONFLICT,
            _ if lower.contains("permission denied") => ERR_PERMISSION,
            _ if CONFLICT_MARKERS.iter().any(|marker| lower.contains(marker)) => ERR_CONFLICT,
            _ => ERR_FAILED,
        };
        return SynexError::Docker(code, message.clone());
    }

    SynexError::Docker(ERR_FAILED, err.to_string())
}

fn timed_out(what: &str, limit: Duration) -> SynexError {
    SynexError::Docker(
        ERR_TIMEOUT,
        format!("{} did not finish within {} s", what, limit.as_secs()),
    )
}

// ===== Разбор полей docker =====

fn rfc3339_secs(secs: i64) -> Option<String> {
    DateTime::<Utc>::from_timestamp(secs, 0)
        .map(|dt| dt.to_rfc3339_opts(SecondsFormat::Secs, true))
}

// "(healthy)" / "(unhealthy)" / "(health: starting)" внутри поля Status.
fn health_of(status: &str) -> Option<String> {
    for (needle, value) in [
        ("(healthy)", "healthy"),
        ("(unhealthy)", "unhealthy"),
        ("(health: starting)", "starting"),
    ] {
        if status.contains(needle) {
            return Some(value.to_string());
        }
    }
    None
}

// "Exited (137) 5 minutes ago" -> 137.
fn exit_code_of(status: &str) -> Option<i32> {
    let rest = status.strip_prefix("Exited (")?;
    let (code, _) = rest.split_once(')')?;
    code.trim().parse().ok()
}

fn port_infos(ports: Option<Vec<bollard::models::PortSummary>>) -> Vec<PortInfo> {
    let mut result = Vec::new();
    for port in ports.unwrap_or_default() {
        result.push(PortInfo {
            container_port: port.private_port.to_string(),
            protocol: port
                .typ
                .map(|value| value.to_string())
                .unwrap_or_else(|| "tcp".to_string()),
            host_ip: port.ip.filter(|ip| !ip.is_empty()),
            host_port: port.public_port.map(|value| value.to_string()),
        });
    }
    result
}

fn container_info(summary: ContainerSummary) -> ContainerInfo {
    let names: Vec<String> = summary
        .names
        .unwrap_or_default()
        .into_iter()
        .map(|name| name.trim_start_matches('/').to_string())
        .collect();

    let id = summary.id.unwrap_or_default();
    let name = names
        .first()
        .cloned()
        .unwrap_or_else(|| id.chars().take(12).collect());

    let labels = summary.labels.unwrap_or_default();
    let status = summary.status.unwrap_or_default();
    let health = health_of(&status);
    let exit_code = exit_code_of(&status);

    let networks = summary
        .network_settings
        .and_then(|settings| settings.networks)
        .map(|map| map.into_keys().collect())
        .unwrap_or_default();

    ContainerInfo {
        id,
        name,
        names,
        image: summary.image.unwrap_or_default(),
        command: summary.command.unwrap_or_default(),
        created_at: summary.created.and_then(rfc3339_secs),
        state: summary.state.map(|state| state.to_string()).unwrap_or_default(),
        status,
        health,
        exit_code,
        ports: port_infos(summary.ports),
        compose_project: labels.get(COMPOSE_PROJECT).cloned(),
        compose_service: labels.get(COMPOSE_SERVICE).cloned(),
        labels,
        networks,
    }
}

fn image_info(summary: ImageSummary) -> ImageInfo {
    let id = summary.id.trim_start_matches("sha256:").to_string();

    // repo_tags: ["nginx:latest", "<none>:<none>"]. Берём первый нормальный тег.
    let mut repository = None;
    let mut tag = None;
    for full in summary.repo_tags {
        if full.starts_with("<none>") {
            continue;
        }
        let (repo, tag_part) = match full.rsplit_once(':') {
            Some((repo, tag_part)) if !tag_part.contains('/') => (repo.to_string(), tag_part),
            _ => (full.clone(), "latest"),
        };
        repository = Some(repo);
        tag = Some(tag_part.to_string());
        break;
    }

    let digest = summary
        .repo_digests
        .first()
        .and_then(|digest| digest.split('@').nth(1).map(|value| value.to_string()));

    ImageInfo {
        id,
        repository,
        tag,
        digest,
        created_at: rfc3339_secs(summary.created),
        size: Some(summary.size),
    }
}

// ===== Клиент =====

// ===== Разбор логов =====

// Docker с timestamps: true ставит RFC3339-время в начало каждой строки.
fn is_log_time(value: &str) -> bool {
    let bytes = value.as_bytes();
    if bytes.len() < 20 {
        return false;
    }
    bytes[4] == b'-'
        && bytes[7] == b'-'
        && bytes[10] == b'T'
        && bytes[13] == b':'
        && bytes[16] == b':'
        && value.ends_with('Z')
}

#[derive(Default)]
struct LogParser {
    lines: Vec<LogLineInfo>,
    // Хвост недописанной строки: docker может разрезать строку между кадрами.
    leftover: String,
}

impl LogParser {
    fn feed(&mut self, stream: &'static str, text: &str) {
        let mut chunk = std::mem::take(&mut self.leftover);
        chunk.push_str(text);

        let mut parts = chunk.split('\n').collect::<Vec<_>>();
        // Последний кусок без перевода строки может быть недописан — откладываем.
        self.leftover = parts.pop().unwrap_or_default().to_string();

        for line in parts {
            let line = line.strip_suffix('\r').unwrap_or(line);
            if line.is_empty() {
                continue;
            }
            let (time, rest) = match line.split_once(' ') {
                Some((time, rest)) if is_log_time(time) => (time.to_string(), rest.to_string()),
                _ => {
                    // Продолжение предыдущей строки (например, \r-прогресс без \n).
                    if let Some(previous) = self.lines.last_mut() {
                        previous.text.push('\n');
                        previous.text.push_str(line);
                    }
                    continue;
                }
            };
            self.lines.push(LogLineInfo {
                time,
                stream,
                text: rest,
            });
        }
    }

    fn into_batch(mut self) -> LogsBatch {
        if !self.leftover.is_empty() {
            // Недописанный хвост отдаём как строку: лучше показать, чем потерять.
            let tail = std::mem::take(&mut self.leftover);
            if let Some(previous) = self.lines.last_mut() {
                previous.text.push('\n');
                previous.text.push_str(&tail);
            }
        }
        let next_since = self.lines.last().map(|line| line.time.clone());
        LogsBatch {
            lines: self.lines,
            next_since,
        }
    }
}

// ===== Статистика =====

fn cpu_percent(previous: &ContainerStatsResponse, current: &ContainerStatsResponse) -> Option<f64> {
    let prev_cpu = previous.cpu_stats.as_ref()?;
    let cur_cpu = current.cpu_stats.as_ref()?;

    let prev_usage = prev_cpu.cpu_usage.as_ref()?.total_usage?;
    let cur_usage = cur_cpu.cpu_usage.as_ref()?.total_usage?;
    let prev_system = prev_cpu.system_cpu_usage?;
    let cur_system = cur_cpu.system_cpu_usage?;

    if cur_usage < prev_usage || cur_system <= prev_system {
        return None;
    }

    let cpu_delta = (cur_usage - prev_usage) as f64;
    let system_delta = (cur_system - prev_system) as f64;
    if system_delta <= 0.0 {
        return None;
    }

    let online = cur_cpu.online_cpus.filter(|value| *value > 0).or_else(|| {
        cur_cpu
            .cpu_usage
            .as_ref()
            .and_then(|usage| usage.percpu_usage.as_ref())
            .map(|percpu| percpu.len() as u32)
            .filter(|value| *value > 0)
    });

    let cpus = online.unwrap_or(1) as f64;
    Some((cpu_delta / system_delta) * cpus * 100.0)
}

fn stats_info(sample: &ContainerStatsResponse, previous: Option<&ContainerStatsResponse>) -> ContainerStatsInfo {
    let memory_used = sample
        .memory_stats
        .as_ref()
        .and_then(|memory| memory.usage);
    let memory_limit = sample
        .memory_stats
        .as_ref()
        .and_then(|memory| memory.limit);
    let memory_percent = match (memory_used, memory_limit) {
        (Some(used), Some(limit)) if limit > 0 => Some(used as f64 / limit as f64 * 100.0),
        _ => None,
    };

    let (net_rx, net_tx) = sample
        .networks
        .as_ref()
        .map(|networks| {
            let mut rx = 0u64;
            let mut tx = 0u64;
            for stats in networks.values() {
                rx += stats.rx_bytes.unwrap_or(0);
                tx += stats.tx_bytes.unwrap_or(0);
            }
            (Some(rx), Some(tx))
        })
        .unwrap_or((None, None));

    let mut block_read = None;
    let mut block_write = None;
    if let Some(entries) = sample
        .blkio_stats
        .as_ref()
        .and_then(|blkio| blkio.io_service_bytes_recursive.as_ref())
    {
        let mut read = 0u64;
        let mut write = 0u64;
        for entry in entries {
            let value = entry.value.unwrap_or(0);
            match entry.op.as_deref() {
                Some(op) if op.eq_ignore_ascii_case("read") => read += value,
                Some(op) if op.eq_ignore_ascii_case("write") => write += value,
                _ => {}
            }
        }
        block_read = Some(read);
        block_write = Some(write);
    }

    ContainerStatsInfo {
        id: sample.id.clone().unwrap_or_default(),
        name: sample.name.clone().unwrap_or_default(),
        cpu_percent: previous.and_then(|prev| cpu_percent(prev, sample)),
        memory_used,
        memory_limit,
        memory_percent,
        net_rx,
        net_tx,
        block_read,
        block_write,
        pids: sample.pids_stats.as_ref().and_then(|pids| pids.current),
    }
}

// ===== Публичные действия =====

// Версии Docker. «Нет Docker», «нет прав» и «демон не запущен» — это ответ, а не сбой:
// статус уезжает в JSON, конверт ответа остаётся ok.
pub async fn info(session: &SessionHandle, socket: &str) -> Result<DockerInfo> {
    let client = make_client(session.clone(), socket)?;

    let version: std::result::Result<SystemVersion, SynexError> =
        match tokio::time::timeout(INFO_TIMEOUT, client.version()).await {
            Ok(Ok(version)) => Ok(version),
            Ok(Err(err)) => Err(map_docker_error(err)),
            Err(_) => Err(timed_out("docker version", INFO_TIMEOUT)),
        };

    match version {
        Ok(version) => Ok(DockerInfo {
            status: "ok",
            detail: None,
            client_version: None,
            server_version: version.version,
            api_version: version.api_version,
            os: version.os,
            arch: version.arch,
        }),
        Err(err) => {
            let (status, detail) = probe_socket(session, socket, &err.to_string()).await;
            Ok(DockerInfo {
                status,
                detail: Some(detail),
                client_version: None,
                server_version: None,
                api_version: None,
                os: None,
                arch: None,
            })
        }
    }
}

// Почему демон недоступен: смотрим сокет и наличие docker на сервере.
// Через exec, потому что ошибка открытия канала в SSH не несёт errno.
async fn probe_socket(
    session: &SessionHandle,
    socket: &str,
    cause: &str,
) -> (&'static str, String) {
    let quoted = format!("'{}'", socket.replace('\'', "'\"'\"'"));
    let command = format!(
        "if [ -S {q} ]; then if [ -w {q} ]; then echo synex:sock=ok; else echo synex:sock=denied; fi; \
         else echo synex:sock=missing; fi; \
         if command -v docker >/dev/null 2>&1; then echo synex:bin=yes; else echo synex:bin=no; fi",
        q = quoted
    );

    let output = {
        let mut guard = session.lock().await;
        guard.execute_command(&command).await
    };

    let stdout = output.map(|output| output.stdout).unwrap_or_default();
    let sock = stdout
        .lines()
        .find_map(|line| line.trim().strip_prefix("synex:sock="))
        .unwrap_or("unknown");
    let bin = stdout
        .lines()
        .find_map(|line| line.trim().strip_prefix("synex:bin="))
        .unwrap_or("unknown");

    let detail = format!("{} (socket: {}, docker binary: {})", cause, sock, bin);

    match sock {
        "missing" if bin == "no" => (ERR_NOT_INSTALLED, detail),
        "missing" => (ERR_DAEMON, detail),
        "denied" => (ERR_PERMISSION, detail),
        _ => (ERR_DAEMON, detail),
    }
}

pub async fn containers(
    session: &SessionHandle,
    socket: &str,
    all: bool,
) -> Result<Vec<ContainerInfo>> {
    let client = make_client(session.clone(), socket)?;
    let options = ListContainersOptions {
        all,
        ..Default::default()
    };

    let list = tokio::time::timeout(REQUEST_TIMEOUT, client.list_containers(Some(options)))
        .await
        .map_err(|_| timed_out("docker ps", REQUEST_TIMEOUT))?
        .map_err(map_docker_error)?;

    Ok(list.into_iter().map(container_info).collect())
}

pub async fn images(session: &SessionHandle, socket: &str) -> Result<Vec<ImageInfo>> {
    let client = make_client(session.clone(), socket)?;

    let options = bollard::query_parameters::ListImagesOptions::default();
    let list = tokio::time::timeout(REQUEST_TIMEOUT, client.list_images(Some(options)))
        .await
        .map_err(|_| timed_out("docker images", REQUEST_TIMEOUT))?
        .map_err(map_docker_error)?;

    Ok(list.into_iter().map(image_info).collect())
}

pub async fn container_action(
    session: &SessionHandle,
    socket: &str,
    container: &str,
    action: &str,
    timeout: Option<i32>,
) -> Result<ContainerActionOutcome> {
    let client = make_client(session.clone(), socket)?;

    let result = tokio::time::timeout(REQUEST_TIMEOUT, async {
        match action {
            "start" => client.start_container(container, None).await,
            "stop" => {
                let options = StopContainerOptions {
                    t: timeout,
                    ..Default::default()
                };
                client.stop_container(container, Some(options)).await
            }
            "restart" => {
                let options = RestartContainerOptions {
                    t: timeout,
                    ..Default::default()
                };
                client.restart_container(container, Some(options)).await
            }
            "pause" => client.pause_container(container).await,
            "unpause" => client.unpause_container(container).await,
            "kill" => client.kill_container(container, None).await,
            other => {
                return Err(SynexError::Docker(
                    ERR_FAILED,
                    format!("unknown container action: {}", other),
                ));
            }
        }
        .map_err(map_docker_error)
    })
    .await
    .map_err(|_| timed_out("docker container action", REQUEST_TIMEOUT))?;

    result?;

    // Состояние после действия: берём свежий список и находим контейнер.
    let list = containers(session, socket, true).await?;
    let found = list
        .into_iter()
        .find(|item| {
            item.id == container
                || item.name == container
                || item.names.iter().any(|name| name == container)
        })
        .ok_or_else(|| {
            SynexError::Docker(
                ERR_NOT_FOUND,
                format!("no such container: {}", container),
            )
        })?;

    Ok(ContainerActionOutcome {
        output: found.name.clone(),
        container: found,
        action: action.to_string(),
    })
}

pub async fn container_remove(
    session: &SessionHandle,
    socket: &str,
    container: &str,
    force: bool,
    volumes: bool,
) -> Result<()> {
    let client = make_client(session.clone(), socket)?;
    let options = RemoveContainerOptions {
        force,
        v: volumes,
        ..Default::default()
    };

    tokio::time::timeout(
        REQUEST_TIMEOUT,
        client.remove_container(container, Some(options)),
    )
    .await
    .map_err(|_| timed_out("docker rm", REQUEST_TIMEOUT))?
    .map_err(map_docker_error)?;

    Ok(())
}

// Логи без follow: одна порция строк.
pub async fn logs(session: &SessionHandle, socket: &str, request: &LogsRequest) -> Result<LogsBatch> {
    let client = make_client(session.clone(), socket)?;
    let options = logs_options(request);

    let mut stream = client.logs(&request.container, Some(options));
    let mut parser = LogParser::default();

    let read = tokio::time::timeout(REQUEST_TIMEOUT, async {
        while let Some(item) = stream.next().await {
            match item {
                Ok(LogOutput::StdOut { message }) => {
                    parser.feed("stdout", &String::from_utf8_lossy(&message));
                }
                Ok(LogOutput::StdErr { message }) => {
                    parser.feed("stderr", &String::from_utf8_lossy(&message));
                }
                Ok(_) => {}
                Err(err) => return Err(map_docker_error(err)),
            }
        }
        Ok::<(), SynexError>(())
    })
    .await
    .map_err(|_| timed_out("docker logs", REQUEST_TIMEOUT))?;

    read?;
    Ok(parser.into_batch())
}

// Логи с follow: строки уезжают пачками, пока не отменят.
// Работает в своей задаче и отдаёт пачки через канал, чтобы вызывающий код
// мог параллельно отправлять кадры и не держать сессию занятой.
pub fn logs_follow(
    session: SessionHandle,
    socket: String,
    request: LogsRequest,
    cancel: CancellationToken,
) -> mpsc::Receiver<Result<LogsBatch>> {
    let (tx, rx) = mpsc::channel(8);

    tokio::spawn(async move {
        let client = match make_client(session, &socket) {
            Ok(client) => client,
            Err(err) => {
                let _ = tx.send(Err(err)).await;
                return;
            }
        };

        let mut stream = client.logs(&request.container, Some(logs_options(&request)));
        let mut parser = LogParser::default();

        loop {
            tokio::select! {
                biased;
                _ = cancel.cancelled() => {
                    let _ = tx.send(Err(SynexError::Cancelled)).await;
                    return;
                }
                item = stream.next() => {
                    match item {
                        Some(Ok(LogOutput::StdOut { message })) => {
                            parser.feed("stdout", &String::from_utf8_lossy(&message));
                        }
                        Some(Ok(LogOutput::StdErr { message })) => {
                            parser.feed("stderr", &String::from_utf8_lossy(&message));
                        }
                        Some(Ok(_)) => continue,
                        Some(Err(err)) => {
                            let _ = tx.send(Err(map_docker_error(err))).await;
                            return;
                        }
                        // Демон закрыл поток (контейнер остановился).
                        None => break,
                    }

                    if parser.lines.is_empty() {
                        continue;
                    }

                    // Отдаём накопленное, начиная новый парсер с хвостом.
                    let batch = std::mem::take(&mut parser).into_batch();
                    parser = LogParser {
                        lines: Vec::new(),
                        leftover: String::new(),
                    };
                    if tx.send(Ok(batch)).await.is_err() {
                        return;
                    }
                }
            }
        }

        let batch = parser.into_batch();
        if !batch.lines.is_empty() {
            let _ = tx.send(Ok(batch)).await;
        }
    });

    rx
}

fn logs_options(request: &LogsRequest) -> LogsOptions {
    LogsOptions {
        follow: request.follow,
        stdout: true,
        stderr: true,
        since: request
            .since
            .as_deref()
            .and_then(parse_since)
            .map(|value| value.clamp(0, i32::MAX as i64) as i32)
            .unwrap_or(0),
        until: 0,
        timestamps: true,
        tail: request
            .tail
            .clone()
            .filter(|tail| !tail.is_empty())
            .unwrap_or_else(|| "all".to_string()),
    }
}

// since принимает unix-секунды, RFC3339 и длительность вида 10m / 1h30m.
fn parse_since(value: &str) -> Option<i64> {
    let value = value.trim();
    if value.is_empty() {
        return None;
    }
    if let Ok(secs) = value.parse::<i64>() {
        return Some(secs);
    }
    if let Ok(duration) = parse_duration(value) {
        return Some(Utc::now().timestamp() - duration);
    }
    if let Ok(dt) = DateTime::parse_from_rfc3339(value) {
        return Some(dt.timestamp());
    }
    if let Ok(dt) = NaiveDateTime::parse_from_str(value, "%Y-%m-%dT%H:%M:%S") {
        return Some(dt.and_utc().timestamp());
    }
    None
}

// "30s", "10m", "1h30m", "2d" -> секунды.
fn parse_duration(value: &str) -> std::result::Result<i64, ()> {
    let mut total: i64 = 0;
    let mut number: i64 = 0;
    let mut has_digit = false;

    for ch in value.chars() {
        if ch.is_ascii_digit() {
            number = number.saturating_mul(10).saturating_add((ch as u8 - b'0') as i64);
            has_digit = true;
            continue;
        }
        if !has_digit {
            return Err(());
        }
        let factor = match ch {
            's' => 1,
            'm' => 60,
            'h' => 3600,
            'd' => 86400,
            'w' => 604800,
            _ => return Err(()),
        };
        total = total.saturating_add(number.saturating_mul(factor));
        number = 0;
        has_digit = false;
    }

    if has_digit {
        return Err(());
    }
    Ok(total)
}

// Нагрузка контейнеров. Docker считает CPU по разнице двух замеров,
// поэтому по каждому контейнеру берём две порции подряд (около секунды между ними).
pub async fn stats(
    session: &SessionHandle,
    socket: &str,
    container: Option<String>,
) -> Result<Vec<ContainerStatsInfo>> {
    let targets = match container {
        Some(name) => vec![name],
        // Без имени — все запущенные: у Docker API нет «stats всех», CLI сам обходит контейнеры.
        None => containers(session, socket, false)
            .await?
            .into_iter()
            .map(|item| item.id)
            .collect(),
    };

    let mut result = Vec::new();
    for target in targets {
        if let Some(info) = stats_one(session, socket, &target).await? {
            result.push(info);
        }
    }

    Ok(result)
}

// Два замера одного контейнера: второй даёт CPU, первый нужен как база.
async fn stats_one(
    session: &SessionHandle,
    socket: &str,
    container: &str,
) -> Result<Option<ContainerStatsInfo>> {
    let client = make_client(session.clone(), socket)?;
    let mut stream = client.stats(container, None);

    let deadline = tokio::time::Instant::now() + STATS_TIMEOUT;
    let mut first: Option<ContainerStatsResponse> = None;
    let mut second: Option<ContainerStatsResponse> = None;

    while second.is_none() {
        let next = tokio::time::timeout_at(deadline, stream.next()).await;
        let sample = match next {
            Err(_) => break,
            Ok(None) => break,
            Ok(Some(Err(err))) => return Err(map_docker_error(err)),
            Ok(Some(Ok(sample))) => sample,
        };

        match first {
            None => first = Some(sample),
            Some(_) => second = Some(sample),
        }
    }

    match (first, second) {
        (Some(current), Some(previous)) => Ok(Some(stats_info(&current, Some(&previous)))),
        // Один замер: память и сеть есть, CPU посчитать не из чего.
        (Some(sample), None) => Ok(Some(stats_info(&sample, None))),
        (None, _) => Err(SynexError::Docker(
            ERR_TIMEOUT,
            format!("docker stats returned no samples for {}", container),
        )),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn exit_code_is_parsed_from_status() {
        assert_eq!(exit_code_of("Exited (137) 5 minutes ago"), Some(137));
        assert_eq!(exit_code_of("Exited (0) 2 hours ago"), Some(0));
        assert_eq!(exit_code_of("Up 5 minutes"), None);
    }

    #[test]
    fn health_is_parsed_from_status() {
        assert_eq!(health_of("Up 5 minutes (healthy)").as_deref(), Some("healthy"));
        assert_eq!(
            health_of("Up 5 seconds (health: starting)").as_deref(),
            Some("starting")
        );
        assert_eq!(health_of("Up 5 minutes"), None);
    }

    #[test]
    fn log_lines_are_parsed_with_timestamps() {
        let mut parser = LogParser::default();
        parser.feed(
            "stdout",
            "2026-11-06T16:05:18.637602832Z line one\n2026-11-06T16:05:19.000000001Z line two\n",
        );
        let batch = parser.into_batch();

        assert_eq!(batch.lines.len(), 2);
        assert_eq!(batch.lines[0].text, "line one");
        assert_eq!(batch.lines[1].stream, "stdout");
        assert_eq!(
            batch.next_since.as_deref(),
            Some("2026-11-06T16:05:19.000000001Z")
        );
    }

    #[test]
    fn log_chunk_without_newline_is_glued_to_next_chunk() {
        let mut parser = LogParser::default();
        parser.feed("stdout", "2026-11-06T16:05:18.000000000Z hel");
        parser.feed("stdout", "lo\n");
        let batch = parser.into_batch();

        assert_eq!(batch.lines.len(), 1);
        assert_eq!(batch.lines[0].text, "hello");
    }

    #[test]
    fn since_accepts_unix_rfc3339_and_duration() {
        assert_eq!(parse_since("1700000000"), Some(1_700_000_000));

        // Один и тот же момент в UTC и со смещением зоны.
        assert_eq!(
            parse_since("2026-11-06T12:00:00Z"),
            parse_since("2026-11-06T15:00:00+03:00")
        );
        assert!(parse_since("2026-11-06T12:00:00Z") < parse_since("2026-11-07T00:00:00Z"));

        let now = Utc::now().timestamp();
        let ten_minutes_ago = parse_since("10m").unwrap();
        assert!((now - 600 - ten_minutes_ago).abs() <= 2);
        let hour_and_half = parse_since("1h30m").unwrap();
        assert!((now - 5400 - hour_and_half).abs() <= 2);
    }
}
