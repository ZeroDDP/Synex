use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::env;
use std::future::Future;
use std::pin::Pin;
use std::sync::Arc;
use std::time::Instant;
use synex_core::errors::{Result as SynexResult, SynexError};
use synex_core::sftp::Sftp;
use synex_core::ssh::SshSession;
use synex_core::docker;
// use tokio::fs::OpenOptions;
use tokio::io::{AsyncRead, AsyncWrite};
use tokio::io::{AsyncReadExt, AsyncWriteExt, WriteHalf};
use tokio::sync::Mutex;
use tokio_util::sync::CancellationToken;
use tracing::{self, error, info};

// Общий тип для пишущей половины соединения. Обёрнут в Arc<Mutex<..>>, чтобы основной
// цикл чтения IPC и спавленные задачи для долгих действий могли писать ответы независимо.
type SharedWriter<T> = Arc<Mutex<WriteHalf<T>>>;

// Токены отмены по идентификатору сессии: сессия обрабатывает одно действие за раз
// (защищена своим Arc<Mutex<SshSession>>), поэтому на сессию всегда есть не больше одного активного токена.
type CancelTokens = Arc<Mutex<HashMap<String, CancellationToken>>>;

// Действие над сессией, упакованное в задачу: сам код получает уже залоченную сессию.
type SessionAction<'a> = Pin<Box<dyn Future<Output = SynexResult<ResponsePayload>> + Send + 'a>>;

fn ok_payload(stdout: String) -> ResponsePayload {
    ResponsePayload {
        stdout,
        stderr: String::new(),
        exit_code: 0,
    }
}

// Сообщение про отсутствующую сессию (повторяется во многих действиях).
const SESSION_NOT_FOUND: &str = "Session dont founded by key. Create session first!";
// use uuid::Uuid;
// use serde::Serialize;
// use tokio::net::TcpListener;

#[derive(Deserialize, Debug)]
struct Request {
    id: String,
    action: String,
    #[serde(default)]
    payload: RequestPayload,
}

#[derive(Deserialize, Debug, Default)]
struct RequestPayload {
    command: Option<String>,
    connection: Option<ConnectPayload>,
    remote_path: Option<String>,
    local_path: Option<String>,
    destination_path: Option<String>,
    // Для действия "cancel": id сессии, чью текущую операцию надо остановить.
    target_id: Option<String>,

    // Docker
    all: Option<bool>,
    container: Option<String>,
    action: Option<String>,
    timeout: Option<i32>,
    tail: Option<u32>,
    since: Option<String>,
    follow: Option<bool>,
    force: Option<bool>,
    volumes: Option<bool>,
}

#[derive(Serialize, Debug)]
struct Response {
    id: String,
    status: String,
    payload: ResponsePayload,
}

#[derive(Serialize, Debug)]
struct ResponsePayload {
    stdout: String,
    stderr: String,
    exit_code: i32,
}

// Ответ на ssh_connect: кроме обычных полей несёт отпечаток сервера для TOFU.
#[derive(Serialize, Debug)]
struct ConnectResponse {
    id: String,
    status: String,
    payload: ConnectResponsePayload,
}

#[derive(Serialize, Debug)]
struct ConnectResponsePayload {
    stdout: String,
    stderr: String,
    exit_code: i32,
    #[serde(skip_serializing_if = "Option::is_none")]
    fingerprint: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    key_type: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    expected_fingerprint: Option<String>,
}

#[derive(Serialize)]
pub struct MetricsResponse {
    pub ok: bool,
    pub cpu_usage_percent: f64,
    pub mem_total_kb: u64,
    pub mem_available_kb: u64,
    pub mem_used_kb: u64,
    pub mem_usage_percent: f64,
    pub net_rx_bytes_total: Option<u64>,
    pub net_tx_bytes_total: Option<u64>,
    pub net_rx_bytes_per_sec: Option<f64>,
    pub net_tx_bytes_per_sec: Option<f64>,
    pub disk_total_kb: Option<u64>,
    pub disk_used_kb: Option<u64>,
    pub disk_available_kb: Option<u64>,
    pub disk_usage_percent: Option<f64>,
    pub error: Option<String>,
}

#[derive(Deserialize)]
struct ConnectPayload {
    host: String,
    port: u16,
    username: String,
    key_path: Option<String>,
    passphrase: Option<String>,
    password: Option<String>,
    host_key_fingerprint: Option<String>,
}

// Пароль и passphrase не должны попадать в логи через {:?}.
impl std::fmt::Debug for ConnectPayload {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("ConnectPayload")
            .field("host", &self.host)
            .field("port", &self.port)
            .field("username", &self.username)
            .field("key_path", &self.key_path)
            .field(
                "passphrase",
                &self.passphrase.as_ref().map(|_| "<redacted>"),
            )
            .field("password", &self.password.as_ref().map(|_| "<redacted>"))
            .field("host_key_fingerprint", &self.host_key_fingerprint)
            .finish()
    }
}

// Больший кадр считаем ошибкой протокола: иначе клиент может выделить до 4 ГБ памяти.
const MAX_FRAME_LEN: usize = 16 * 1024 * 1024;

#[tokio::main]
async fn main() {
    tracing_subscriber::fmt()
        .with_writer(std::io::stderr)
        .init();
    let tcp_port: u16 = env::var("RUST_IPC_TCP_FALLBACK_PORT")
        .unwrap_or_else(|_| "9100".to_string())
        .parse()
        .unwrap_or(9100);

    #[cfg(windows)]
    {
        let pipe_name =
            env::var("RUST_IPC_PIPE_NAME").unwrap_or_else(|_| r"\\.\pipe\synex_worker".to_string());
        if let Err(e) = run_named_pipe(pipe_name).await {
            eprintln!("Pipe failed ({}), falling back to TCP", e);
            let _ = run_tcp(tcp_port).await;
        }
    }

    #[cfg(unix)]
    {
        let sock_path = env::var("RUST_IPC_SOCKET_PATH")
            .unwrap_or_else(|_| "/tmp/synex_worker.sock".to_string());
        if let Err(e) = run_unix_socket(sock_path).await {
            eprintln!("Socket failed ({}), falling back to TCP", e);
            let _ = run_tcp(tcp_port).await;
        }
    }
}

async fn handle_client<T>(stream: T)
where
    T: AsyncRead + AsyncWrite + Unpin + Send + 'static,
{
    let sessions: Arc<Mutex<HashMap<String, Arc<Mutex<SshSession>>>>> =
        Arc::new(Mutex::new(HashMap::<String, Arc<Mutex<SshSession>>>::new()));
    let cancel_tokens: CancelTokens = Arc::new(Mutex::new(HashMap::new()));

    // Чтение остаётся в этом цикле, запись расшарена на Arc<Mutex<..>>: долгие действия
    // (ssh_exec, SFTP-передачи) выполняются в своих задачах, не блокируя чтение следующего кадра
    // (в том числе действия cancel).
    let (mut reader, writer) = tokio::io::split(stream);
    let writer: SharedWriter<T> = Arc::new(Mutex::new(writer));

    let mut len_buf = [0u8; 4];

    loop {
        if reader.read_exact(&mut len_buf).await.is_err() {
            break;
        }
        let payload_len = u32::from_be_bytes(len_buf) as usize;
        if payload_len > MAX_FRAME_LEN {
            send_error(
                &writer,
                "",
                &format!("frame too large: {} bytes", payload_len),
            )
            .await;
            break;
        }

        let mut payload_buf = vec![0u8; payload_len];
        if reader.read_exact(&mut payload_buf).await.is_err() {
            break;
        }

        let req = match rmp_serde::from_slice::<Request>(&payload_buf) {
            Ok(r) => r,
            Err(e) => {
                send_error(&writer, "bad_msgpack", &format!("Failed to unpack: {}", e)).await;
                continue;
            }
        };

        // Обрабатываем действия
        match req.action.as_str() {
            "ssh_exec" => {
                info!("Обработка ssh_exec");

                // Распаковка ArcMutex списка
                let session_arc = match get_session(&sessions, &req.id).await {
                    Some(session) => session,
                    None => {
                        error!("Session dont founded by key: {}", &req.id);
                        send_error(
                            &writer,
                            &req.id,
                            "Session dont founded by key. Create session first!",
                        )
                        .await;
                        continue;
                    }
                };

                let command: String = match req.payload.command {
                    Some(cmd) => cmd,
                    None => {
                        send_error(&writer, &req.id, "Missing 'command' in payload").await;
                        continue;
                    }
                };

                // Токен регистрируем до запуска задачи: если cancel придёт пока мы ждём
                // очереди на сессию, операция сразу увидит отмену и не станет выполняться.
                let token = CancellationToken::new();
                cancel_tokens
                    .lock()
                    .await
                    .insert(req.id.clone(), token.clone());

                let writer = writer.clone();
                let cancel_tokens = cancel_tokens.clone();
                let req_id = req.id.clone();

                // Спавленная задача: основной цикл тут же возвращается читать следующий кадр.
                tokio::spawn(async move {
                    info!("Выполняется команда: {}", command);
                    let result = {
                        let mut sess = session_arc.lock().await;
                        sess.execute_command_cancellable(&command, &token).await
                    };
                    cancel_tokens.lock().await.remove(&req_id);

                    match result {
                        Ok(output) => {
                            info!("Команда выполнена. Exit code: {}", output.exit_code);
                            send_response(
                                &writer,
                                Response {
                                    id: req_id,
                                    status: "ok".to_string(),
                                    payload: ResponsePayload {
                                        stdout: output.stdout,
                                        stderr: output.stderr,
                                        exit_code: output.exit_code,
                                    },
                                },
                            )
                            .await;
                        }
                        Err(SynexError::Cancelled) => {
                            info!("Команда отменена: {}", req_id);
                            send_response(
                                &writer,
                                Response {
                                    id: req_id,
                                    status: "cancelled".to_string(),
                                    payload: ResponsePayload {
                                        stdout: String::new(),
                                        stderr: String::new(),
                                        // 128 + SIGTERM(15): процесс остановлен по запросу cancel
                                        exit_code: 143,
                                    },
                                },
                            )
                            .await;
                        }
                        Err(e) => {
                            error!("❌ Ошибка выполнения команды: {}", e);
                            send_error(&writer, &req_id, &e.to_string()).await;
                        }
                    }
                });
            }
            "ssh_connect" => {
                info!("Обработка ssh_connect");
                let p: ConnectPayload = match req.payload.connection {
                    Some(conn) => conn,
                    None => {
                        eprintln!("Missing 'connection' in payload");
                        send_error(&writer, &req.id, "Missing 'connection' in payload").await;
                        continue;
                    }
                };

                info!(
                    "Попытка подключения к {}@{}:{} (key: {}, password задан: {}, host key закреплён: {})...",
                    p.username,
                    p.host,
                    p.port,
                    p.key_path.as_deref().unwrap_or("None"),
                    p.password.is_some(),
                    p.host_key_fingerprint.is_some()
                );

                // let session_id = Uuid::new_v4().to_string();

                match SshSession::connect(
                    &p.host,
                    p.port,
                    &p.username,
                    &p.key_path,
                    &p.passphrase,
                    &p.password,
                    &p.host_key_fingerprint,
                )
                .await
                {
                    Ok(sess) => {
                        info!("SSH-подключение успешно!");
                        let host_key = sess.host_key.clone();
                        let session_arc = Arc::new(Mutex::new(sess));
                        sessions.lock().await.insert(req.id.clone(), session_arc);
                        let resp = ConnectResponse {
                            id: req.id.clone(),
                            status: "ok".to_string(),
                            payload: ConnectResponsePayload {
                                stdout: format!("Connected successfully, id: {}", req.id),
                                stderr: String::new(),
                                exit_code: 0,
                                fingerprint: Some(host_key.fingerprint),
                                key_type: Some(host_key.key_type),
                                expected_fingerprint: None,
                            },
                        };
                        info!("Отправляю ответ об успешном подключении...");
                        send_response(&writer, resp).await;
                        info!("Ответ отправлен");
                    }
                    Err(e) => {
                        eprintln!("❌ Ошибка SSH-подключения: {}", e);
                        // Для host_key_* stderr начинается с кода ошибки, остальные ошибки идут с префиксом
                        let mut payload = ConnectResponsePayload {
                            stdout: String::new(),
                            stderr: format!("Connection failed: {}", e),
                            exit_code: 1,
                            fingerprint: None,
                            key_type: None,
                            expected_fingerprint: None,
                        };
                        match &e {
                            SynexError::HostKeyUnknown(key) => {
                                payload.stderr = e.to_string();
                                payload.fingerprint = Some(key.fingerprint.clone());
                                payload.key_type = Some(key.key_type.clone());
                            }
                            SynexError::HostKeyMismatch { expected, actual } => {
                                payload.stderr = e.to_string();
                                payload.fingerprint = Some(actual.fingerprint.clone());
                                payload.key_type = Some(actual.key_type.clone());
                                payload.expected_fingerprint = Some(expected.clone());
                            }
                            _ => {}
                        }
                        send_response(
                            &writer,
                            ConnectResponse {
                                id: req.id.clone(),
                                status: "error".to_string(),
                                payload,
                            },
                        )
                        .await;
                    }
                }
            }
            "monitor_get_values" => {
                info!("Обработка monitor");

                // Распаковка ArcMutex списка

                let session_arc = match get_session(&sessions, &req.id).await {
                    Some(session) => session,
                    None => {
                        error!("Session dont founded by key: {}", &req.id);
                        send_error(
                            &writer,
                            &req.id,
                            "Session dont founded by key. Create session first!",
                        )
                        .await;
                        continue;
                    }
                };

                info!("Выполняется команда мониторинга");
                spawn_session_action(writer.clone(), session_arc, req.id.clone(), |sess| {
                    Box::pin(async move {
                        let command = "head -n 1 /proc/stat; grep -E 'MemTotal|MemAvailable' /proc/meminfo; cat /proc/net/dev; df -P -k /";
                        let output = sess.execute_command(command).await?;
                        // Разбираем поля сессии по отдельности, иначе два &mut через DerefMut конфликтуют
                        let state = &mut *sess;
                        let stdout_json = parse_metrics_to_json(
                            &output.stdout,
                            &mut state.prev_cpu,
                            &mut state.prev_net,
                        );
                        Ok(ResponsePayload {
                            stdout: stdout_json,
                            stderr: output.stderr,
                            exit_code: output.exit_code,
                        })
                    })
                })
                .await;
            }
            "ssh_disconnect" => {
                info!("Обработка ssh_disconnect для id: {}", req.id);
                // Отменяем любую операцию, ещё идущую на этой сессии, до закрытия.
                if let Some(token) = cancel_tokens.lock().await.remove(&req.id) {
                    token.cancel();
                }
                let removed = sessions.lock().await.remove(&req.id);

                match removed {
                    Some(sess_arc) => {
                        let id = req.id.clone();
                        // Задача ждёт лок сессии: пока отменённая операция завершается, закрывать рано.
                        spawn_session_action(
                            writer.clone(),
                            sess_arc,
                            req.id.clone(),
                            move |sess| {
                                Box::pin(async move {
                                    let _ = sess.close().await; // метод из ssh.rs
                                    Ok(ok_payload(format!("Disconnected id: {}", id)))
                                })
                            },
                        )
                        .await;
                    }
                    None => {
                        send_error(&writer, &req.id, "Session not found").await;
                    }
                }
            }
            "sftp_list_dir" => {
                let session_arc = match get_session(&sessions, &req.id).await {
                    Some(session) => session,
                    None => {
                        error!("Session dont founded by key: {}", &req.id);
                        send_error(
                            &writer,
                            &req.id,
                            "Session dont founded by key. Create session first!",
                        )
                        .await;
                        continue;
                    }
                };

                let path = match req.payload.remote_path.clone() {
                    Some(path) => path,
                    None => {
                        send_error(&writer, &req.id, "Missing directory path in payload").await;
                        continue;
                    }
                };
                spawn_session_action(writer.clone(), session_arc, req.id.clone(), move |sess| {
                    Box::pin(async move {
                        let mut sftp = Sftp::new(sess);
                        let entries = sftp.list_directory_entries(&path).await?;
                        let stdout =
                            serde_json::to_string(&entries).unwrap_or_else(|_| "[]".to_string());
                        Ok(ok_payload(stdout))
                    })
                })
                .await;
            }
            "sftp_stat" => {
                let session_arc = match get_session(&sessions, &req.id).await {
                    Some(session) => session,
                    None => {
                        error!("Session dont founded by key: {}", &req.id);
                        send_error(
                            &writer,
                            &req.id,
                            "Session dont founded by key. Create session first!",
                        )
                        .await;
                        continue;
                    }
                };
                let path = match req.payload.remote_path.clone() {
                    Some(path) => path,
                    None => {
                        send_error(&writer, &req.id, "Missing path in payload").await;
                        continue;
                    }
                };
                spawn_session_action(writer.clone(), session_arc, req.id.clone(), move |sess| {
                    Box::pin(async move {
                        let mut sftp = Sftp::new(sess);
                        let entry = sftp.stat(&path).await?;
                        let stdout =
                            serde_json::to_string(&entry).unwrap_or_else(|_| "{}".to_string());
                        Ok(ok_payload(stdout))
                    })
                })
                .await;
            }
            "sftp_remove_file" => {
                let session_arc = match get_session(&sessions, &req.id).await {
                    Some(session) => session,
                    None => {
                        error!("Session dont founded by key: {}", &req.id);
                        send_error(
                            &writer,
                            &req.id,
                            "Session dont founded by key. Create session first!",
                        )
                        .await;
                        continue;
                    }
                };
                let path = match req.payload.remote_path.clone() {
                    Some(path) => path,
                    None => {
                        send_error(&writer, &req.id, "Missing file path in payload").await;
                        continue;
                    }
                };
                spawn_session_action(writer.clone(), session_arc, req.id.clone(), move |sess| {
                    Box::pin(async move {
                        let mut sftp = Sftp::new(sess);
                        sftp.remove_file(&path).await?;
                        Ok(ok_payload(String::new()))
                    })
                })
                .await;
            }
            "sftp_remove_dir" => {
                let session_arc = match get_session(&sessions, &req.id).await {
                    Some(session) => session,
                    None => {
                        error!("Session dont founded by key: {}", &req.id);
                        send_error(
                            &writer,
                            &req.id,
                            "Session dont founded by key. Create session first!",
                        )
                        .await;
                        continue;
                    }
                };
                let path = match req.payload.remote_path.clone() {
                    Some(path) => path,
                    None => {
                        send_error(&writer, &req.id, "Missing directory path in payload").await;
                        continue;
                    }
                };
                spawn_session_action(writer.clone(), session_arc, req.id.clone(), move |sess| {
                    Box::pin(async move {
                        let mut sftp = Sftp::new(sess);
                        sftp.remove_directory(&path).await?;
                        Ok(ok_payload(String::new()))
                    })
                })
                .await;
            }
            "sftp_create_dir" => {
                let session_arc = match get_session(&sessions, &req.id).await {
                    Some(session) => session,
                    None => {
                        error!("Session dont founded by key: {}", &req.id);
                        send_error(
                            &writer,
                            &req.id,
                            "Session dont founded by key. Create session first!",
                        )
                        .await;
                        continue;
                    }
                };
                let path = match req.payload.remote_path.clone() {
                    Some(path) => path,
                    None => {
                        send_error(&writer, &req.id, "Missing directory path in payload").await;
                        continue;
                    }
                };
                spawn_session_action(writer.clone(), session_arc, req.id.clone(), move |sess| {
                    Box::pin(async move {
                        let mut sftp = Sftp::new(sess);
                        sftp.create_directory(&path).await?;
                        Ok(ok_payload(String::new()))
                    })
                })
                .await;
            }
            "sftp_upload_file" => {
                let session_arc = match get_session(&sessions, &req.id).await {
                    Some(session) => session,
                    None => {
                        error!("Session dont founded by key: {}", &req.id);
                        send_error(
                            &writer,
                            &req.id,
                            "Session dont founded by key. Create session first!",
                        )
                        .await;
                        continue;
                    }
                };
                let local_path = match req.payload.local_path.clone() {
                    Some(path) => path,
                    None => {
                        send_error(&writer, &req.id, "Missing file path in payload").await;
                        continue;
                    }
                };
                let remote_path = match req.payload.remote_path.clone() {
                    Some(path) => path,
                    None => {
                        send_error(&writer, &req.id, "Missing file content in payload").await;
                        continue;
                    }
                };

                let token = CancellationToken::new();
                cancel_tokens
                    .lock()
                    .await
                    .insert(req.id.clone(), token.clone());

                let writer = writer.clone();
                let cancel_tokens = cancel_tokens.clone();
                let req_id = req.id.clone();

                tokio::spawn(async move {
                    let result = {
                        let mut sess = session_arc.lock().await;
                        let mut sftp = Sftp::new(&mut *sess);
                        sftp.upload_file_cancellable(&local_path, &remote_path, &token)
                            .await
                    };
                    cancel_tokens.lock().await.remove(&req_id);

                    match result {
                        Ok(_) => {
                            send_response(
                                &writer,
                                Response {
                                    id: req_id,
                                    status: "ok".to_string(),
                                    payload: ResponsePayload {
                                        stdout: String::new(),
                                        stderr: String::new(),
                                        exit_code: 0,
                                    },
                                },
                            )
                            .await;
                        }
                        Err(SynexError::Cancelled) => {
                            send_response(
                                &writer,
                                Response {
                                    id: req_id,
                                    status: "cancelled".to_string(),
                                    payload: ResponsePayload {
                                        stdout: String::new(),
                                        stderr: String::new(),
                                        exit_code: 0,
                                    },
                                },
                            )
                            .await;
                        }
                        Err(e) => send_error(&writer, &req_id, &e.to_string()).await,
                    }
                });
            }
            "sftp_download_file" => {
                let session_arc = match get_session(&sessions, &req.id).await {
                    Some(session) => session,
                    None => {
                        error!("Session dont founded by key: {}", &req.id);
                        send_error(
                            &writer,
                            &req.id,
                            "Session dont founded by key. Create session first!",
                        )
                        .await;
                        continue;
                    }
                };
                let remote_path = match req.payload.remote_path.clone() {
                    Some(path) => path,
                    None => {
                        send_error(&writer, &req.id, "Missing file path in payload").await;
                        continue;
                    }
                };
                let local_path = match req.payload.local_path.clone() {
                    Some(path) => path,
                    None => {
                        send_error(&writer, &req.id, "Missing file content in payload").await;
                        continue;
                    }
                };

                let token = CancellationToken::new();
                cancel_tokens
                    .lock()
                    .await
                    .insert(req.id.clone(), token.clone());

                let writer = writer.clone();
                let cancel_tokens = cancel_tokens.clone();
                let req_id = req.id.clone();

                tokio::spawn(async move {
                    let result = {
                        let mut sess = session_arc.lock().await;
                        let mut sftp = Sftp::new(&mut *sess);
                        sftp.download_file_cancellable(&remote_path, &local_path, &token)
                            .await
                    };
                    cancel_tokens.lock().await.remove(&req_id);

                    match result {
                        Ok(_) => {
                            send_response(
                                &writer,
                                Response {
                                    id: req_id,
                                    status: "ok".to_string(),
                                    payload: ResponsePayload {
                                        stdout: String::new(),
                                        stderr: String::new(),
                                        exit_code: 0,
                                    },
                                },
                            )
                            .await;
                        }
                        Err(SynexError::Cancelled) => {
                            send_response(
                                &writer,
                                Response {
                                    id: req_id,
                                    status: "cancelled".to_string(),
                                    payload: ResponsePayload {
                                        stdout: String::new(),
                                        stderr: String::new(),
                                        exit_code: 0,
                                    },
                                },
                            )
                            .await;
                        }
                        Err(e) => send_error(&writer, &req_id, &e.to_string()).await,
                    }
                });
            }
            "sftp_move_file" => {
                if req.payload.remote_path.is_none() || req.payload.destination_path.is_none() {
                    send_error(&writer, &req.id, "Missing source or target in payload").await;
                    continue;
                }
                let session_arc = match get_session(&sessions, &req.id).await {
                    Some(session) => session,
                    None => {
                        error!("Session dont founded by key: {}", &req.id);
                        send_error(
                            &writer,
                            &req.id,
                            "Session dont founded by key. Create session first!",
                        )
                        .await;
                        continue;
                    }
                };
                let source = req.payload.remote_path.clone().unwrap_or_default();
                let destination = req.payload.destination_path.clone().unwrap_or_default();
                spawn_session_action(writer.clone(), session_arc, req.id.clone(), move |sess| {
                    Box::pin(async move {
                        let mut sftp = Sftp::new(sess);
                        sftp.move_file(&source, &destination).await?;
                        Ok(ok_payload(String::new()))
                    })
                })
                .await;
            }
            "sftp_move_dir" => {
                if req.payload.remote_path.is_none() || req.payload.destination_path.is_none() {
                    send_error(&writer, &req.id, "Missing source or target in payload").await;
                    continue;
                }
                let session_arc = match get_session(&sessions, &req.id).await {
                    Some(session) => session,
                    None => {
                        error!("Session dont founded by key: {}", &req.id);
                        send_error(
                            &writer,
                            &req.id,
                            "Session dont founded by key. Create session first!",
                        )
                        .await;
                        continue;
                    }
                };
                let source = req.payload.remote_path.clone().unwrap_or_default();
                let destination = req.payload.destination_path.clone().unwrap_or_default();
                spawn_session_action(writer.clone(), session_arc, req.id.clone(), move |sess| {
                    Box::pin(async move {
                        let mut sftp = Sftp::new(sess);
                        sftp.move_dir(&source, &destination).await?;
                        Ok(ok_payload(String::new()))
                    })
                })
                .await;
            }
            "sftp_copy_file" => {
                if req.payload.remote_path.is_none() || req.payload.destination_path.is_none() {
                    send_error(&writer, &req.id, "Missing source or destination in payload").await;
                    continue;
                }
                let session_arc = match get_session(&sessions, &req.id).await {
                    Some(session) => session,
                    None => {
                        error!("Session dont founded by key: {}", &req.id);
                        send_error(
                            &writer,
                            &req.id,
                            "Session dont founded by key. Create session first!",
                        )
                        .await;
                        continue;
                    }
                };
                let source = req.payload.remote_path.clone().unwrap_or_default();
                let destination = req.payload.destination_path.clone().unwrap_or_default();
                spawn_session_action(writer.clone(), session_arc, req.id.clone(), move |sess| {
                    Box::pin(async move {
                        let mut sftp = Sftp::new(sess);
                        sftp.copy_file(&source, &destination).await?;
                        Ok(ok_payload(String::new()))
                    })
                })
                .await;
            }
            "docker_info" => {
                let session_arc = match get_session(&sessions, &req.id).await {
                    Some(session) => session,
                    None => {
                        send_error(&writer, &req.id, SESSION_NOT_FOUND).await;
                        continue;
                    }
                };
                let writer = writer.clone();
                let req_id = req.id.clone();
                tokio::spawn(async move {
                    let result = docker::info(&session_arc, docker::DEFAULT_SOCKET).await;
                    send_docker_result(&writer, req_id, result).await;
                });
            }
            "docker_containers" => {
                let session_arc = match get_session(&sessions, &req.id).await {
                    Some(session) => session,
                    None => {
                        send_error(&writer, &req.id, SESSION_NOT_FOUND).await;
                        continue;
                    }
                };
                let all = req.payload.all.unwrap_or(false);
                let writer = writer.clone();
                let req_id = req.id.clone();
                tokio::spawn(async move {
                    let result =
                        docker::containers(&session_arc, docker::DEFAULT_SOCKET, all).await;
                    send_docker_result(&writer, req_id, result).await;
                });
            }
            "docker_images" => {
                let session_arc = match get_session(&sessions, &req.id).await {
                    Some(session) => session,
                    None => {
                        send_error(&writer, &req.id, SESSION_NOT_FOUND).await;
                        continue;
                    }
                };
                let writer = writer.clone();
                let req_id = req.id.clone();
                tokio::spawn(async move {
                    let result = docker::images(&session_arc, docker::DEFAULT_SOCKET).await;
                    send_docker_result(&writer, req_id, result).await;
                });
            }
            "docker_stats" => {
                let session_arc = match get_session(&sessions, &req.id).await {
                    Some(session) => session,
                    None => {
                        send_error(&writer, &req.id, SESSION_NOT_FOUND).await;
                        continue;
                    }
                };
                let container = req.payload.container.clone();
                let writer = writer.clone();
                let req_id = req.id.clone();
                tokio::spawn(async move {
                    let result =
                        docker::stats(&session_arc, docker::DEFAULT_SOCKET, container).await;
                    send_docker_result(&writer, req_id, result).await;
                });
            }
            "docker_container_action" => {
                let session_arc = match get_session(&sessions, &req.id).await {
                    Some(session) => session,
                    None => {
                        send_error(&writer, &req.id, SESSION_NOT_FOUND).await;
                        continue;
                    }
                };
                let container = match req.payload.container.clone() {
                    Some(container) if !container.is_empty() => container,
                    _ => {
                        send_error(&writer, &req.id, "Missing 'container' in payload").await;
                        continue;
                    }
                };
                let action = match req.payload.action.clone() {
                    Some(action) if !action.is_empty() => action,
                    _ => {
                        send_error(&writer, &req.id, "Missing 'action' in payload").await;
                        continue;
                    }
                };
                let timeout = req.payload.timeout;
                let writer = writer.clone();
                let req_id = req.id.clone();
                tokio::spawn(async move {
                    let result = docker::container_action(
                        &session_arc,
                        docker::DEFAULT_SOCKET,
                        &container,
                        &action,
                        timeout,
                    )
                    .await;
                    send_docker_result(&writer, req_id, result).await;
                });
            }
            "docker_container_remove" => {
                let session_arc = match get_session(&sessions, &req.id).await {
                    Some(session) => session,
                    None => {
                        send_error(&writer, &req.id, SESSION_NOT_FOUND).await;
                        continue;
                    }
                };
                let container = match req.payload.container.clone() {
                    Some(container) if !container.is_empty() => container,
                    _ => {
                        send_error(&writer, &req.id, "Missing 'container' in payload").await;
                        continue;
                    }
                };
                let force = req.payload.force.unwrap_or(false);
                let volumes = req.payload.volumes.unwrap_or(false);
                let writer = writer.clone();
                let req_id = req.id.clone();
                tokio::spawn(async move {
                    let result = docker::container_remove(
                        &session_arc,
                        docker::DEFAULT_SOCKET,
                        &container,
                        force,
                        volumes,
                    )
                    .await;
                    match result {
                        Ok(()) => send_response(&writer, Response { id: req_id, status: "ok".to_string(), payload: ok_payload(String::new()) }).await,
                        Err(err) => send_error(&writer, &req_id, &err.to_string()).await,
                    }
                });
            }
            "docker_logs" => {
                let session_arc = match get_session(&sessions, &req.id).await {
                    Some(session) => session,
                    None => {
                        send_error(&writer, &req.id, SESSION_NOT_FOUND).await;
                        continue;
                    }
                };
                let container = match req.payload.container.clone() {
                    Some(container) if !container.is_empty() => container,
                    _ => {
                        send_error(&writer, &req.id, "Missing 'container' in payload").await;
                        continue;
                    }
                };

                let logs_request = docker::LogsRequest {
                    container,
                    tail: req.payload.tail.map(|tail| tail.to_string()),
                    since: req.payload.since.clone(),
                    follow: req.payload.follow.unwrap_or(false),
                };

                if !logs_request.follow {
                    let writer = writer.clone();
                    let req_id = req.id.clone();
                    tokio::spawn(async move {
                        let result =
                            docker::logs(&session_arc, docker::DEFAULT_SOCKET, &logs_request)
                                .await;
                        send_docker_result(&writer, req_id, result).await;
                    });
                    continue;
                }

                // follow: промежуточные кадры status="progress", пока не отменят.
                let token = CancellationToken::new();
                cancel_tokens
                    .lock()
                    .await
                    .insert(req.id.clone(), token.clone());

                let writer = writer.clone();
                let cancel_tokens = cancel_tokens.clone();
                let req_id = req.id.clone();
                tokio::spawn(async move {
                    let mut batches = docker::logs_follow(
                        session_arc,
                        docker::DEFAULT_SOCKET.to_string(),
                        logs_request,
                        token,
                    );

                    let mut cancelled = false;
                    while let Some(item) = batches.recv().await {
                        match item {
                            Ok(batch) => {
                                let stdout = serde_json::to_string(&batch)
                                    .unwrap_or_else(|_| "{}".to_string());
                                send_response(
                                    &writer,
                                    Response {
                                        id: req_id.clone(),
                                        status: "progress".to_string(),
                                        payload: ok_payload(stdout),
                                    },
                                )
                                .await;
                            }
                            Err(SynexError::Cancelled) => {
                                cancelled = true;
                                break;
                            }
                            Err(err) => {
                                cancel_tokens.lock().await.remove(&req_id);
                                send_error(&writer, &req_id, &err.to_string()).await;
                                return;
                            }
                        }
                    }

                    cancel_tokens.lock().await.remove(&req_id);
                    let status = if cancelled { "cancelled" } else { "ok" };
                    send_response(
                        &writer,
                        Response {
                            id: req_id,
                            status: status.to_string(),
                            payload: ok_payload(String::new()),
                        },
                    )
                    .await;
                });
            }
            "cancel" => {
                // Отмена текущей операции на сессии target_id (ssh_exec или SFTP-передача).
                // Сама отменяемая операция ответит отдельно, своим исходным id, со status: "cancelled".
                let target = match req.payload.target_id.clone() {
                    Some(target) => target,
                    None => {
                        send_error(&writer, &req.id, "Missing 'target_id' in payload").await;
                        continue;
                    }
                };
                let token = cancel_tokens.lock().await.get(&target).cloned();
                match token {
                    Some(token) => {
                        token.cancel();
                        send_response(
                            &writer,
                            Response {
                                id: req.id,
                                status: "ok".to_string(),
                                payload: ResponsePayload {
                                    stdout: format!("Cancel requested for {}", target),
                                    stderr: String::new(),
                                    exit_code: 0,
                                },
                            },
                        )
                        .await;
                    }
                    None => {
                        send_error(
                            &writer,
                            &req.id,
                            &format!("No cancellable operation for id: {}", target),
                        )
                        .await;
                    }
                }
            }
            _ => {
                eprintln!("❌ Неизвестное действие: {}", &req.action);
                send_error(
                    &writer,
                    &req.id,
                    &format!("Unknown action: {}", &req.action),
                )
                .await;
            }
        }
        info!("Цикл завершен, жду следующее сообщение...\n");

        info!("Обработка клиента завершена");
    }

    // При закрытии IPC-подключения отменяем все его действия и закрываем все его сессии.
    for (_, token) in cancel_tokens.lock().await.drain() {
        token.cancel();
    }
    let mut store = sessions.lock().await;
    for (id, sess_arc) in store.drain() {
        info!("Авто-закрытие сессии {} после отключения Python", id);
        let mut sess = sess_arc.lock().await;
        let _ = sess.close().await;
    }
}

async fn send_error<T: AsyncWrite + Unpin>(writer: &SharedWriter<T>, id: &str, msg: &str) {
    let resp = Response {
        id: id.to_string(),
        status: "error".to_string(),
        payload: ResponsePayload {
            stdout: String::new(),
            stderr: msg.to_string(),
            exit_code: 1,
        },
    };
    send_response(writer, resp).await;
}

// Пишет под общим Mutex: основной цикл и спавленные задачи могут отвечать одновременно,
// лок не даёт их кадрам перемешаться в потоке.
async fn send_response<T: AsyncWrite + Unpin, R: Serialize>(writer: &SharedWriter<T>, resp: R) {
    if let Ok(packed) = rmp_serde::to_vec_named(&resp) {
        let len = (packed.len() as u32).to_be_bytes();
        let mut stream = writer.lock().await;
        let _ = stream.write_all(&len).await;
        let _ = stream.write_all(&packed).await;
        let _ = stream.flush().await;
    }
}

// Ответ Docker-действия: JSON в stdout или код docker_* в stderr.
async fn send_docker_result<T, R>(writer: &SharedWriter<T>, req_id: String, result: SynexResult<R>)
where
    T: AsyncWrite + Unpin,
    R: Serialize,
{
    match result {
        Ok(value) => {
            let stdout = serde_json::to_string(&value).unwrap_or_else(|_| "null".to_string());
            send_response(
                writer,
                Response {
                    id: req_id,
                    status: "ok".to_string(),
                    payload: ok_payload(stdout),
                },
            )
            .await;
        }
        Err(err) => send_error(writer, &req_id, &err.to_string()).await,
    }
}

// Выполняет действие над сессией в отдельной задаче.
// Важно: лок сессии берётся внутри задачи, поэтому цикл чтения IPC не ждёт, пока
// освободится занятая сессия (например, пока идёт `sleep 100`), и `cancel` доходит.
async fn spawn_session_action<T, F>(
    writer: SharedWriter<T>,
    session_arc: Arc<Mutex<SshSession>>,
    req_id: String,
    action: F,
) where
    T: AsyncWrite + Unpin + Send + 'static,
    F: for<'a> FnOnce(&'a mut SshSession) -> SessionAction<'a> + Send + 'static,
{
    tokio::spawn(async move {
        let result = {
            let mut sess = session_arc.lock().await;
            action(&mut sess).await
        };

        match result {
            Ok(payload) => {
                send_response(
                    &writer,
                    Response {
                        id: req_id,
                        status: "ok".to_string(),
                        payload,
                    },
                )
                .await;
            }
            Err(e) => send_error(&writer, &req_id, &e.to_string()).await,
        }
    });
}

// async fn send_response(stream: &mut tokio::net::TcpStream, resp: Response) {
//     let packed = rmp_serde::to_vec_named(&resp).unwrap();
//     let len = (packed.len() as u32).to_be_bytes();

//     stream.write_all(&len).await.unwrap();
//     stream.write_all(&packed).await.unwrap();
//     stream.flush().await.unwrap();
// }

// Windows: named pipe
#[cfg(windows)]
async fn run_named_pipe(pipe_name: String) -> std::io::Result<()> {
    use tokio::net::windows::named_pipe::ServerOptions;

    println!("🔌 Listening on pipe: {}", pipe_name);

    // Первый сервер
    let mut server = ServerOptions::new()
        .first_pipe_instance(true)
        .create(&pipe_name)?;

    loop {
        // Ждём подключения клиента
        server.connect().await?;
        let client = server; // отдаём этого клиента в обработку

        // Создаём новый сервер для следующих подключений
        server = ServerOptions::new().create(&pipe_name)?;

        tokio::spawn(handle_client(client));
    }
}
// fn init_winsock() {
//     use std::os::windows::io::AsRawSocket;
//     use windows_sys::Win32::Networking::WinSock::{MAKEWORD, WSADATA, WSAStartup};

//     unsafe {
//         let mut wsa_data: WSADATA = std::mem::zeroed();
//         let result = WSAStartup(MAKEWORD(2, 2), &mut wsa_data);
//         if result != 0 {
//             eprintln!("WSAStartup failed with error: {}", result);
//         }
//     }
// }

// Linux/Mac: unix socket
#[cfg(unix)]
async fn run_unix_socket(path: String) -> std::io::Result<()> {
    use std::os::unix::fs::PermissionsExt;
    use tokio::net::UnixListener;
    let _ = std::fs::remove_file(&path); // чистим старый файл сокета
    let listener = UnixListener::bind(&path)?;
    // Сокет доступен только владельцу: другие локальные пользователи не должны подключаться к воркеру
    std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600))?;
    println!("🔌 Listening on socket: {}", path);
    loop {
        let (stream, _) = listener.accept().await?;
        tokio::spawn(handle_client(stream));
    }
}

// Fallback: tcp
async fn run_tcp(port: u16) -> std::io::Result<()> {
    use tokio::net::TcpListener;
    let listener = TcpListener::bind(("127.0.0.1", port)).await?;
    println!("🔌 Listening on tcp 127.0.0.1:{}", port);
    loop {
        let (stream, _) = listener.accept().await?;
        tokio::spawn(handle_client(stream));
    }
}

// Строка интерфейса из /proc/net/dev: "eth0: rx_bytes ... tx_bytes ...". Возвращает (rx, tx).
// Loopback не считаем: это внутренний трафик машины.
fn parse_net_dev_line(line: &str) -> Option<(u64, u64)> {
    let (name, rest) = line.split_once(':')?;
    let name = name.trim();
    if name.is_empty() || name == "lo" || name.contains(char::is_whitespace) {
        return None;
    }
    let nums: Vec<u64> = rest
        .split_whitespace()
        .map(|s| s.parse::<u64>())
        .collect::<std::result::Result<_, _>>()
        .ok()?;
    // rx_bytes — поле 0, tx_bytes — поле 8
    if nums.len() < 9 {
        return None;
    }
    Some((nums[0], nums[8]))
}

// Строка `df -P -k`: Filesystem, 1K-blocks, Used, Available, Capacity, Mounted on.
// Возвращает (total_kb, used_kb, available_kb).
fn parse_df_line(line: &str) -> Option<(u64, u64, u64)> {
    let tokens: Vec<&str> = line.split_whitespace().collect();
    if tokens.len() < 6 || !tokens[4].ends_with('%') {
        return None;
    }
    Some((
        tokens[1].parse().ok()?,
        tokens[2].parse().ok()?,
        tokens[3].parse().ok()?,
    ))
}

fn parse_metrics_to_json(
    raw: &str,
    prev_cpu: &mut Option<(u64, u64)>,
    prev_net: &mut Option<(u64, u64, Instant)>,
) -> String {
    let make_err = |msg: &str| -> String {
        serde_json::to_string(&MetricsResponse {
            ok: false,
            cpu_usage_percent: 0.0,
            mem_total_kb: 0,
            mem_available_kb: 0,
            mem_used_kb: 0,
            mem_usage_percent: 0.0,
            net_rx_bytes_total: None,
            net_tx_bytes_total: None,
            net_rx_bytes_per_sec: None,
            net_tx_bytes_per_sec: None,
            disk_total_kb: None,
            disk_used_kb: None,
            disk_available_kb: None,
            disk_usage_percent: None,
            error: Some(msg.to_string()),
        })
        .unwrap_or_else(|_| r#"{"ok":false,"error":"serialization_failed"}"#.to_string())
    };

    if raw.trim().is_empty() {
        return make_err("Пустой ответ от сервера (команда не выполнилась)");
    }

    let mut cur_cpu: Option<(u64, u64)> = None;
    let mut mem_total: Option<u64> = None;
    let mut mem_avail: Option<u64> = None;
    let mut net_totals: Option<(u64, u64)> = None;
    let mut disk: Option<(u64, u64, u64)> = None;

    for line in raw.lines() {
        let line = line.trim();
        if line.starts_with("cpu ") {
            let nums: Vec<u64> = line
                .split_whitespace()
                .skip(1)
                .filter_map(|s| s.parse::<u64>().ok())
                .collect();

            if nums.len() < 4 {
                return make_err("Битая строка метрик CPU");
            }

            let idle = nums[3] + nums.get(4).copied().unwrap_or(0);
            let total: u64 = nums.iter().sum();
            cur_cpu = Some((idle, total));
        } else if line.starts_with("MemTotal:") {
            mem_total = line.split_whitespace().nth(1).and_then(|v| v.parse().ok());
        } else if line.starts_with("MemAvailable:") {
            mem_avail = line.split_whitespace().nth(1).and_then(|v| v.parse().ok());
        } else if let Some((rx, tx)) = parse_net_dev_line(line) {
            // Суммируем все интерфейсы, кроме loopback
            let (sum_rx, sum_tx) = net_totals.unwrap_or((0, 0));
            net_totals = Some((sum_rx + rx, sum_tx + tx));
        } else if let Some(parsed) = parse_df_line(line) {
            disk = Some(parsed);
        }
    }

    let Some((cur_idle, cur_total)) = cur_cpu else {
        return make_err("Метрики CPU не найдены в выводе");
    };

    // Считаем дельту с прошлым запросом этой же сессии
    let cpu_percent = if let Some((old_idle, old_total)) = *prev_cpu {
        let total_d = cur_total.saturating_sub(old_total);
        let idle_d = cur_idle.saturating_sub(old_idle);
        if total_d == 0 {
            0.0
        } else {
            ((total_d.saturating_sub(idle_d)) as f64 / total_d as f64) * 100.0
        }
    } else {
        // Первый запрос в сессии — считаем среднюю, дальше пойдет чистая дельта
        if cur_total == 0 {
            0.0
        } else {
            ((cur_total.saturating_sub(cur_idle)) as f64 / cur_total as f64) * 100.0
        }
    };

    // Сохраняем текущие тики для следующего вызова
    *prev_cpu = Some((cur_idle, cur_total));

    let (Some(total_kb), Some(avail_kb)) = (mem_total, mem_avail) else {
        return make_err("Метрики MemTotal или MemAvailable не найдены");
    };

    if total_kb == 0 {
        return make_err("MemTotal равен 0");
    }

    let used_kb = total_kb.saturating_sub(avail_kb);
    let mem_percent = (used_kb as f64 / total_kb as f64) * 100.0;

    let round2 = |val: f64| (val * 100.0).round() / 100.0;

    // Скорость сети — дельта счётчиков за время между запросами этой сессии.
    // На первом запросе скорости нет (null), как и CPU на первом запросе.
    let now = Instant::now();
    let (net_rx_total, net_tx_total, net_rx_rate, net_tx_rate) = match net_totals {
        Some((rx, tx)) => {
            let rates = match *prev_net {
                Some((old_rx, old_tx, at)) => {
                    let secs = now.duration_since(at).as_secs_f64();
                    (secs > 0.0).then(|| {
                        (
                            round2(rx.saturating_sub(old_rx) as f64 / secs),
                            round2(tx.saturating_sub(old_tx) as f64 / secs),
                        )
                    })
                }
                None => None,
            };
            *prev_net = Some((rx, tx, now));
            (
                Some(rx),
                Some(tx),
                rates.map(|(r, _)| r),
                rates.map(|(_, t)| t),
            )
        }
        None => (None, None, None, None),
    };

    // df считает занятость как used / (used + available), так же считаем и здесь
    let (disk_total, disk_used, disk_avail, disk_percent) = match disk {
        Some((total, used, avail)) => {
            let denom = used + avail;
            let percent = if denom == 0 {
                0.0
            } else {
                used as f64 / denom as f64 * 100.0
            };
            (Some(total), Some(used), Some(avail), Some(round2(percent)))
        }
        None => (None, None, None, None),
    };

    let resp = MetricsResponse {
        ok: true,
        cpu_usage_percent: round2(cpu_percent),
        mem_total_kb: total_kb,
        mem_available_kb: avail_kb,
        mem_used_kb: used_kb,
        mem_usage_percent: round2(mem_percent),
        net_rx_bytes_total: net_rx_total,
        net_tx_bytes_total: net_tx_total,
        net_rx_bytes_per_sec: net_rx_rate,
        net_tx_bytes_per_sec: net_tx_rate,
        disk_total_kb: disk_total,
        disk_used_kb: disk_used,
        disk_available_kb: disk_avail,
        disk_usage_percent: disk_percent,
        error: None,
    };

    serde_json::to_string(&resp).unwrap_or_else(|_| make_err("Ошибка сериализации JSON"))
}

async fn get_session(
    sessions: &Arc<Mutex<HashMap<String, Arc<Mutex<SshSession>>>>>,
    id: &str,
) -> Option<Arc<Mutex<SshSession>>> {
    sessions.lock().await.get(id).cloned()
}

#[cfg(test)]
mod metrics_tests {
    use super::*;

    #[test]
    fn net_dev_line_sums_rx_and_tx() {
        let line = "eth0: 1000 1 0 0 0 0 0 0 2000 2 0 0 0 0 0 0";
        assert_eq!(parse_net_dev_line(line), Some((1000, 2000)));
    }

    #[test]
    fn net_dev_skips_loopback_and_headers() {
        assert_eq!(
            parse_net_dev_line("lo: 1000 1 0 0 0 0 0 0 2000 2 0 0 0 0 0 0"),
            None
        );
        assert_eq!(
            parse_net_dev_line(
                "Inter-|   Receive                                                |  Transmit"
            ),
            None
        );
        assert_eq!(
            parse_net_dev_line(
                " face |bytes    packets errs drop fifo frame compressed multicast|bytes"
            ),
            None
        );
    }

    #[test]
    fn df_line_parses_kb_values() {
        assert_eq!(
            parse_df_line("/dev/sda1 1000 400 600 40% /"),
            Some((1000, 400, 600))
        );
    }

    #[test]
    fn df_header_is_not_a_disk_line() {
        assert_eq!(
            parse_df_line("Filesystem 1024-blocks Used Available Capacity Mounted on"),
            None
        );
    }

    #[test]
    fn metrics_first_call_has_null_net_rates_and_disk_percent() {
        let raw = "cpu  100 0 100 800 0 0 0 0 0 0\n\
MemTotal:       2000 kB\n\
MemAvailable:   1500 kB\n\
eth0: 1000 1 0 0 0 0 0 0 2000 2 0 0 0 0 0 0\n\
/dev/sda1 1000 400 600 40% /";
        let mut prev_cpu = None;
        let mut prev_net = None;
        let json: serde_json::Value =
            serde_json::from_str(&parse_metrics_to_json(raw, &mut prev_cpu, &mut prev_net))
                .unwrap();

        assert_eq!(json["ok"], true);
        assert_eq!(json["net_rx_bytes_total"], 1000);
        assert_eq!(json["net_tx_bytes_total"], 2000);
        assert!(json["net_rx_bytes_per_sec"].is_null());
        assert!(json["net_tx_bytes_per_sec"].is_null());
        assert_eq!(json["disk_total_kb"], 1000);
        assert_eq!(json["disk_used_kb"], 400);
        assert_eq!(json["disk_available_kb"], 600);
        assert_eq!(json["disk_usage_percent"], 40.0);
    }

    #[test]
    fn metrics_without_disk_or_net_return_nulls() {
        let raw =
            "cpu  100 0 100 800 0 0 0 0 0 0\nMemTotal:       2000 kB\nMemAvailable:   1500 kB";
        let mut prev_cpu = None;
        let mut prev_net = None;
        let json: serde_json::Value =
            serde_json::from_str(&parse_metrics_to_json(raw, &mut prev_cpu, &mut prev_net))
                .unwrap();

        assert_eq!(json["ok"], true);
        assert!(json["net_rx_bytes_total"].is_null());
        assert!(json["disk_usage_percent"].is_null());
    }
}
