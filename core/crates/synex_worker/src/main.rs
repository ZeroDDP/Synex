use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::sync::Arc;
use std::time::Instant;
use std::{env, result};
use synex_core::errors::SynexError;
use synex_core::mon::SshMonitoring;
use synex_core::sftp::Sftp;
use synex_core::ssh::SshSession;
// use tokio::fs::OpenOptions;
use tokio::io::{AsyncRead, AsyncWrite};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::sync::Mutex;
use tracing::{self, error, info};
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

async fn handle_client<T>(mut stream: T)
where
    T: AsyncRead + AsyncWrite + Unpin + Send + 'static,
{
    let sessions: Arc<Mutex<HashMap<String, Arc<Mutex<SshSession>>>>> =
        Arc::new(Mutex::new(HashMap::<String, Arc<Mutex<SshSession>>>::new()));
    let mut len_buf = [0u8; 4];

    loop {
        if stream.read_exact(&mut len_buf).await.is_err() {
            break;
        }
        let payload_len = u32::from_be_bytes(len_buf) as usize;
        if payload_len > MAX_FRAME_LEN {
            send_error(
                &mut stream,
                "",
                &format!("frame too large: {} bytes", payload_len),
            )
            .await;
            break;
        }

        let mut payload_buf = vec![0u8; payload_len];
        if stream.read_exact(&mut payload_buf).await.is_err() {
            break;
        }

        let req = match rmp_serde::from_slice::<Request>(&payload_buf) {
            Ok(r) => r,
            Err(e) => {
                send_error(
                    &mut stream,
                    "bad_msgpack",
                    &format!("Failed to unpack: {}", e),
                )
                .await;
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
                            &mut stream,
                            &req.id,
                            "Session dont founded by key. Create session first!",
                        )
                        .await;
                        continue;
                    }
                };

                // 2. Теперь лочим саму сессию
                let mut sess = session_arc.lock().await;
                let command: String = match req.payload.command {
                    Some(cmd) => cmd,
                    None => {
                        eprintln!("❌ Отсутствует command в payload");
                        send_error(&mut stream, &req.id, "Missing 'command' in payload").await;
                        continue;
                    }
                };

                info!("Выполняется команда: {}", command);
                match sess.execute_command(&command).await {
                    Ok(output) => {
                        info!("Команда выполнена. Exit code: {}", output.exit_code);
                        let resp = Response {
                            id: req.id.clone(),
                            status: "ok".to_string(),
                            payload: ResponsePayload {
                                stdout: output.stdout,
                                stderr: output.stderr,
                                exit_code: output.exit_code,
                            },
                        };
                        send_response(&mut stream, resp).await;
                    }
                    Err(e) => {
                        error!("❌ Ошибка выполнения команды: {}", e);
                        send_error(&mut stream, &req.id, &e.to_string()).await;
                    }
                }
            }
            "ssh_connect" => {
                info!("Обработка ssh_connect");
                let p: ConnectPayload = match req.payload.connection {
                    Some(conn) => conn,
                    None => {
                        eprintln!("Missing 'connection' in payload");
                        send_error(&mut stream, &req.id, "Missing 'connection' in payload").await;
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
                        send_response(&mut stream, resp).await;
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
                            &mut stream,
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
                            &mut stream,
                            &req.id,
                            "Session dont founded by key. Create session first!",
                        )
                        .await;
                        continue;
                    }
                };

                let mut sess = session_arc.lock().await;

                let command = "head -n 1 /proc/stat; grep -E 'MemTotal|MemAvailable' /proc/meminfo; cat /proc/net/dev; df -P -k /";
                info!("Выполняется команда мониторинга");
                match sess.execute_command(command).await {
                    Ok(output) => {
                        // Разбираем поля сессии по отдельности, иначе два &mut через DerefMut конфликтуют
                        let state = &mut *sess;
                        let stdout_json = parse_metrics_to_json(
                            &output.stdout,
                            &mut state.prev_cpu,
                            &mut state.prev_net,
                        );
                        let resp = Response {
                            id: req.id,
                            status: "ok".to_string(),
                            payload: ResponsePayload {
                                stdout: stdout_json,
                                stderr: output.stderr,
                                exit_code: output.exit_code,
                            },
                        };
                        send_response(&mut stream, resp).await;
                    }
                    Err(e) => {
                        error!("❌ Ошибка выполнения команды: {}", e);
                        send_error(&mut stream, &req.id, &e.to_string()).await;
                    }
                }
            }
            "ssh_disconnect" => {
                info!("Обработка ssh_disconnect для id: {}", req.id);
                let removed = sessions.lock().await.remove(&req.id);

                match removed {
                    Some(sess_arc) => {
                        let mut sess = sess_arc.lock().await;
                        let _ = sess.close().await; // вызываем метод из ssh.rs
                        let resp = Response {
                            id: req.id.clone(),
                            status: "ok".to_string(),
                            payload: ResponsePayload {
                                stdout: format!("Disconnected id: {}", req.id),
                                stderr: String::new(),
                                exit_code: 0,
                            },
                        };
                        send_response(&mut stream, resp).await;
                    }
                    None => {
                        send_error(&mut stream, &req.id, "Session not found").await;
                    }
                }
            }
            "sftp_list_dir" => {
                let session_arc = match get_session(&sessions, &req.id).await {
                    Some(session) => session,
                    None => {
                        error!("Session dont founded by key: {}", &req.id);
                        send_error(
                            &mut stream,
                            &req.id,
                            "Session dont founded by key. Create session first!",
                        )
                        .await;
                        continue;
                    }
                };

                let mut sess = session_arc.lock().await;
                let mut sftp = Sftp::new(&mut *sess);

                let path = match req.payload.remote_path.as_deref() {
                    Some(path) => path,
                    None => {
                        send_error(&mut stream, &req.id, "Missing directory path in payload").await;
                        continue;
                    }
                };
                match sftp.list_directory_entries(path).await {
                    Ok(entries) => {
                        let stdout =
                            serde_json::to_string(&entries).unwrap_or_else(|_| "[]".to_string());
                        send_response(
                            &mut stream,
                            Response {
                                id: req.id,
                                status: "ok".to_string(),
                                payload: ResponsePayload {
                                    stdout,
                                    stderr: String::new(),
                                    exit_code: 0,
                                },
                            },
                        )
                        .await;
                    }
                    Err(e) => send_error(&mut stream, &req.id, &e.to_string()).await,
                }
            }
            "sftp_stat" => {
                let session_arc = match get_session(&sessions, &req.id).await {
                    Some(session) => session,
                    None => {
                        error!("Session dont founded by key: {}", &req.id);
                        send_error(
                            &mut stream,
                            &req.id,
                            "Session dont founded by key. Create session first!",
                        )
                        .await;
                        continue;
                    }
                };
                let mut sess = session_arc.lock().await;
                let mut sftp = Sftp::new(&mut *sess);
                let path = match req.payload.remote_path.as_deref() {
                    Some(path) => path,
                    None => {
                        send_error(&mut stream, &req.id, "Missing path in payload").await;
                        continue;
                    }
                };
                match sftp.stat(path).await {
                    Ok(entry) => {
                        let stdout =
                            serde_json::to_string(&entry).unwrap_or_else(|_| "{}".to_string());
                        send_response(
                            &mut stream,
                            Response {
                                id: req.id,
                                status: "ok".to_string(),
                                payload: ResponsePayload {
                                    stdout,
                                    stderr: String::new(),
                                    exit_code: 0,
                                },
                            },
                        )
                        .await;
                    }
                    Err(e) => send_error(&mut stream, &req.id, &e.to_string()).await,
                }
            }
            "sftp_remove_file" => {
                let session_arc = match get_session(&sessions, &req.id).await {
                    Some(session) => session,
                    None => {
                        error!("Session dont founded by key: {}", &req.id);
                        send_error(
                            &mut stream,
                            &req.id,
                            "Session dont founded by key. Create session first!",
                        )
                        .await;
                        continue;
                    }
                };
                let mut sess = session_arc.lock().await;
                let mut sftp = Sftp::new(&mut *sess);
                let path = match req.payload.remote_path.as_deref() {
                    Some(path) => path,
                    None => {
                        send_error(&mut stream, &req.id, "Missing file path in payload").await;
                        continue;
                    }
                };
                match sftp.remove_file(path).await {
                    Ok(_) => {
                        send_response(
                            &mut stream,
                            Response {
                                id: req.id,
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
                    Err(e) => send_error(&mut stream, &req.id, &e.to_string()).await,
                }
            }
            "sftp_remove_dir" => {
                let session_arc = match get_session(&sessions, &req.id).await {
                    Some(session) => session,
                    None => {
                        error!("Session dont founded by key: {}", &req.id);
                        send_error(
                            &mut stream,
                            &req.id,
                            "Session dont founded by key. Create session first!",
                        )
                        .await;
                        continue;
                    }
                };
                let mut sess = session_arc.lock().await;
                let mut sftp = Sftp::new(&mut *sess);
                let path = match req.payload.remote_path.as_deref() {
                    Some(path) => path,
                    None => {
                        send_error(&mut stream, &req.id, "Missing directory path in payload").await;
                        continue;
                    }
                };
                match sftp.remove_directory(path).await {
                    Ok(_) => {
                        send_response(
                            &mut stream,
                            Response {
                                id: req.id,
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
                    Err(e) => send_error(&mut stream, &req.id, &e.to_string()).await,
                }
            }
            "sftp_create_dir" => {
                let session_arc = match get_session(&sessions, &req.id).await {
                    Some(session) => session,
                    None => {
                        error!("Session dont founded by key: {}", &req.id);
                        send_error(
                            &mut stream,
                            &req.id,
                            "Session dont founded by key. Create session first!",
                        )
                        .await;
                        continue;
                    }
                };
                let mut sess = session_arc.lock().await;
                let mut sftp = Sftp::new(&mut *sess);
                let path = match req.payload.remote_path.as_deref() {
                    Some(path) => path,
                    None => {
                        send_error(&mut stream, &req.id, "Missing directory path in payload").await;
                        continue;
                    }
                };
                match sftp.create_directory(path).await {
                    Ok(_) => {
                        send_response(
                            &mut stream,
                            Response {
                                id: req.id,
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
                    Err(e) => send_error(&mut stream, &req.id, &e.to_string()).await,
                }
            }
            "sftp_upload_file" => {
                let session_arc = match get_session(&sessions, &req.id).await {
                    Some(session) => session,
                    None => {
                        error!("Session dont founded by key: {}", &req.id);
                        send_error(
                            &mut stream,
                            &req.id,
                            "Session dont founded by key. Create session first!",
                        )
                        .await;
                        continue;
                    }
                };
                let mut sess = session_arc.lock().await;
                let mut sftp = Sftp::new(&mut *sess);
                let path = match req.payload.local_path.as_deref() {
                    Some(path) => path,
                    None => {
                        send_error(&mut stream, &req.id, "Missing file path in payload").await;
                        continue;
                    }
                };
                let file = match req.payload.remote_path.as_deref() {
                    Some(file) => file,
                    None => {
                        send_error(&mut stream, &req.id, "Missing file content in payload").await;
                        continue;
                    }
                };
                match sftp.upload_file(path, file).await {
                    Ok(_) => {
                        send_response(
                            &mut stream,
                            Response {
                                id: req.id,
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
                    Err(e) => send_error(&mut stream, &req.id, &e.to_string()).await,
                }
            }
            "sftp_download_file" => {
                let session_arc = match get_session(&sessions, &req.id).await {
                    Some(session) => session,
                    None => {
                        error!("Session dont founded by key: {}", &req.id);
                        send_error(
                            &mut stream,
                            &req.id,
                            "Session dont founded by key. Create session first!",
                        )
                        .await;
                        continue;
                    }
                };
                let mut sess = session_arc.lock().await;
                let mut sftp = Sftp::new(&mut *sess);
                let path = match req.payload.remote_path.as_deref() {
                    Some(path) => path,
                    None => {
                        send_error(&mut stream, &req.id, "Missing file path in payload").await;
                        continue;
                    }
                };
                let file = match req.payload.local_path.as_deref() {
                    Some(file) => file,
                    None => {
                        send_error(&mut stream, &req.id, "Missing file content in payload").await;
                        continue;
                    }
                };
                match sftp.download_file(path, file).await {
                    Ok(_) => {
                        send_response(
                            &mut stream,
                            Response {
                                id: req.id,
                                status: "ok".to_string(),
                                payload: ResponsePayload {
                                    stdout: "".to_string(),
                                    stderr: "".to_string(),
                                    exit_code: 0,
                                },
                            },
                        )
                        .await;
                    }
                    Err(e) => send_error(&mut stream, &req.id, &e.to_string()).await,
                }
            }
            "sftp_move_file" => {
                if req.payload.remote_path.is_none() || req.payload.destination_path.is_none() {
                    send_error(&mut stream, &req.id, "Missing source or target in payload").await;
                    continue;
                }
                let session_arc = match get_session(&sessions, &req.id).await {
                    Some(session) => session,
                    None => {
                        error!("Session dont founded by key: {}", &req.id);
                        send_error(
                            &mut stream,
                            &req.id,
                            "Session dont founded by key. Create session first!",
                        )
                        .await;
                        continue;
                    }
                };
                let mut sess = session_arc.lock().await;
                let mut sftp = Sftp::new(&mut *sess);
                match sftp
                    .move_file(
                        &req.payload.remote_path.unwrap_or_default(),
                        &req.payload.destination_path.unwrap_or_default(),
                    )
                    .await
                {
                    Ok(_) => {
                        send_response(
                            &mut stream,
                            Response {
                                id: req.id,
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
                    Err(e) => send_error(&mut stream, &req.id, &e.to_string()).await,
                }
            }
            "sftp_move_dir" => {
                if req.payload.remote_path.is_none() || req.payload.destination_path.is_none() {
                    send_error(&mut stream, &req.id, "Missing source or target in payload").await;
                    continue;
                }
                let session_arc = match get_session(&sessions, &req.id).await {
                    Some(session) => session,
                    None => {
                        error!("Session dont founded by key: {}", &req.id);
                        send_error(
                            &mut stream,
                            &req.id,
                            "Session dont founded by key. Create session first!",
                        )
                        .await;
                        continue;
                    }
                };
                let mut sess = session_arc.lock().await;
                let mut sftp = Sftp::new(&mut *sess);
                match sftp
                    .move_dir(
                        &req.payload.remote_path.unwrap_or_default(),
                        &req.payload.destination_path.unwrap_or_default(),
                    )
                    .await
                {
                    Ok(_) => {
                        send_response(
                            &mut stream,
                            Response {
                                id: req.id,
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
                    Err(e) => send_error(&mut stream, &req.id, &e.to_string()).await,
                }
            }
            "sftp_copy_file" => {
                if req.payload.remote_path.is_none() || req.payload.destination_path.is_none() {
                    send_error(
                        &mut stream,
                        &req.id,
                        "Missing source or destination in payload",
                    )
                    .await;
                    continue;
                }
                let session_arc = match get_session(&sessions, &req.id).await {
                    Some(session) => session,
                    None => {
                        error!("Session dont founded by key: {}", &req.id);
                        send_error(
                            &mut stream,
                            &req.id,
                            "Session dont founded by key. Create session first!",
                        )
                        .await;
                        continue;
                    }
                };
                let mut sess = session_arc.lock().await;
                let mut sftp = Sftp::new(&mut *sess);
                match sftp
                    .copy_file(
                        &req.payload.remote_path.unwrap_or_default(),
                        &req.payload.destination_path.unwrap_or_default(),
                    )
                    .await
                {
                    Ok(_) => {
                        send_response(
                            &mut stream,
                            Response {
                                id: req.id,
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
                    Err(e) => send_error(&mut stream, &req.id, &e.to_string()).await,
                }
            }
            _ => {
                eprintln!("❌ Неизвестное действие: {}", &req.action);
                send_error(
                    &mut stream,
                    &req.id,
                    &format!("Unknown action: {}", &req.action),
                )
                .await;
            }
        }
        info!("Цикл завершен, жду следующее сообщение...\n");

        info!("Обработка клиента завершена");
    }
    let mut store = sessions.lock().await;
    for (id, sess_arc) in store.drain() {
        info!("Авто-закрытие сессии {} после отключения Python", id);
        let mut sess = sess_arc.lock().await;
        let _ = sess.close().await;
    }
}

async fn send_error<T: AsyncWrite + Unpin>(stream: &mut T, id: &str, msg: &str) {
    let resp = Response {
        id: id.to_string(),
        status: "error".to_string(),
        payload: ResponsePayload {
            stdout: String::new(),
            stderr: msg.to_string(),
            exit_code: 1,
        },
    };
    send_response(stream, resp).await;
}

async fn send_response<T: AsyncWrite + Unpin, R: Serialize>(stream: &mut T, resp: R) {
    if let Ok(packed) = rmp_serde::to_vec_named(&resp) {
        let len = (packed.len() as u32).to_be_bytes();
        let _ = stream.write_all(&len).await;
        let _ = stream.write_all(&packed).await;
        let _ = stream.flush().await;
    }
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
