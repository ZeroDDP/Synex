use russh::keys::{HashAlg, PrivateKeyWithHashAlg};
use russh::{
    client::{self, Handler},
    keys::PublicKeyOrCertificate,
};
use serde::{Deserialize, Serialize};
use std::sync::{Arc, Mutex as StdMutex};
use std::time::{Duration, Instant};
use tokio_util::sync::CancellationToken;

use crate::errors::{Result, SftpErrorKind, SynexError}; // файл ошибок

// Сколько ждать между TERM и KILL при отмене команды.
const KILL_GRACE: Duration = Duration::from_secs(3);
// Сколько ещё ждать закрытия канала после KILL, прежде чем бросить его.
const AFTER_KILL_WAIT: Duration = Duration::from_secs(5);

#[derive(Debug, Serialize, Deserialize)]
pub struct SshOutput {
    pub stdout: String,
    pub stderr: String,
    pub exit_code: i32,
}

// Ключ сервера в виде, в котором он уходит в ответ backend.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct HostKey {
    // OpenSSH-формат: SHA256:<base64 без паддинга>
    pub fingerprint: String,
    // Например, ssh-ed25519 или rsa-sha2-256
    pub key_type: String,
}

// Хендлер отвечает на вопросы сервера.
// Схема TOFU: backend хранит отпечаток, который уже подтвердил пользователь.
// Если отпечатка нет, подключение прерывается с HostKeyUnknown, и backend спрашивает пользователя.
struct ClientHandler {
    expected_fingerprint: Option<String>,
    // Ключ, который сервер показал в этом подключении. Нужен connect, чтобы вернуть его в ответе.
    seen_key: Arc<StdMutex<Option<HostKey>>>,
}

impl Handler for ClientHandler {
    type Error = SynexError;

    async fn check_server_key(
        &mut self,
        server_public_key: &PublicKeyOrCertificate,
    ) -> std::result::Result<bool, Self::Error> {
        let key = server_public_key.public_key();
        let host_key = HostKey {
            fingerprint: key.fingerprint(HashAlg::Sha256).to_string(),
            key_type: key.algorithm().to_string(),
        };

        if let Ok(mut seen) = self.seen_key.lock() {
            *seen = Some(host_key.clone());
        }

        match self.expected_fingerprint.as_deref() {
            None => Err(SynexError::HostKeyUnknown(host_key)),
            Some(expected) if expected == host_key.fingerprint => Ok(true),
            Some(expected) => Err(SynexError::HostKeyMismatch {
                expected: expected.to_string(),
                actual: host_key,
            }),
        }
    }
}

pub struct SshSession {
    session: client::Handle<ClientHandler>,
    pub host_key: HostKey,
    pub prev_cpu: Option<(u64, u64)>,          // idle, total
    pub prev_net: Option<(u64, u64, Instant)>, // rx, tx, момент замера
}

impl SshSession {
    // new connection
    pub async fn connect(
        host: &str,
        port: u16,
        username: &str,
        key_path: &Option<String>,
        passphrase: &Option<String>,
        password: &Option<String>,
        host_key_fingerprint: &Option<String>,
    ) -> Result<Self> {
        let config = Arc::new(client::Config::default());
        let seen_key = Arc::new(StdMutex::new(None));
        let handler = ClientHandler {
            expected_fingerprint: host_key_fingerprint.clone(),
            seen_key: seen_key.clone(),
        };

        // Ключ проверяется во время обмена ключами, до аутентификации,
        // поэтому пароль или passphrase не уходят на неподтверждённый сервер.
        let mut session = client::connect(config, (host, port), handler).await?;

        let auth_result = if let Some(path) = key_path.as_deref() {
            // 1. Пробуем авторизацию по ключу из файла
            let key_pair = russh::keys::load_secret_key(path, passphrase.as_deref())?;
            session
                .authenticate_publickey(
                    username,
                    PrivateKeyWithHashAlg::new(
                        Arc::new(key_pair),
                        session.best_supported_rsa_hash().await?.flatten(),
                    ),
                )
                .await?
        } else if let Some(pass) = password.as_deref() {
            // 2. Если ключа нет, пробуем пароль
            session.authenticate_password(username, pass).await?
        } else {
            // 3. Если ничего не передано — ошибка
            return Err(SynexError::Internal(
                "No authentication method provided".into(),
            ));
        };

        if !auth_result.success() {
            return Err(SynexError::SshAuthFailed);
        }

        let host_key = seen_key
            .lock()
            .ok()
            .and_then(|seen| seen.clone())
            .ok_or_else(|| SynexError::Internal("Server host key was not received".into()))?;

        Ok(SshSession {
            session,
            host_key,
            prev_cpu: None,
            prev_net: None,
        })
    }

    // exec in session using
    pub async fn execute_command(&mut self, command: &str) -> Result<SshOutput> {
        // Обычный вызов — это отменяемый с токеном, который никто не отменит.
        let cancel = CancellationToken::new();
        self.execute_command_cancellable(command, &cancel).await
    }

    // exec с возможностью отмены. При отмене: шлём удалённому процессу TERM,
    // даём KILL_GRACE на завершение, если не уложился — добиваем KILL.
    // В любом случае возвращаем SynexError::Cancelled, а не частичный вывод.
    pub async fn execute_command_cancellable(
        &mut self,
        command: &str,
        cancel: &CancellationToken,
    ) -> Result<SshOutput> {
        // Отмену могли прислать пока запрос ждал сессию: тогда ничего не запускаем.
        if cancel.is_cancelled() {
            return Err(SynexError::Cancelled);
        }

        let mut channel = self.session.channel_open_session().await?;
        channel.exec(true, command).await?;

        let mut stdout = String::new();
        let mut stderr = String::new();
        let mut exit_code = 0;
        let mut was_cancelled = false;
        let mut killed = false;
        let mut kill_at: Option<tokio::time::Instant> = None;

        // Выходим только на Close: ExitStatus приходит после Eof, и на Eof выход потерял бы код.
        loop {
            tokio::select! {
                biased;

                // Пришла отмена: просим процесс завершиться сам, ставим таймер на KILL.
                _ = cancel.cancelled(), if !was_cancelled => {
                    was_cancelled = true;
                    let _ = channel.signal(russh::Sig::TERM).await;
                    kill_at = Some(tokio::time::Instant::now() + KILL_GRACE);
                }

                // За KILL_GRACE процесс не закрыл канал — добиваем. Если и после KILL
                // канал не закрылся, не висим на нём: отдаём отмену и отпускаем канал.
                _ = sleep_until_opt(kill_at), if kill_at.is_some() => {
                    if killed {
                        break;
                    }
                    let _ = channel.signal(russh::Sig::KILL).await;
                    killed = true;
                    kill_at = Some(tokio::time::Instant::now() + AFTER_KILL_WAIT);
                }

                msg = channel.wait() => {
                    match msg {
                        Some(russh::ChannelMsg::Data { data }) => {
                            stdout.push_str(&String::from_utf8_lossy(&data));
                        }
                        Some(russh::ChannelMsg::ExtendedData { data, ext }) => {
                            if ext == 1 {
                                stderr.push_str(&String::from_utf8_lossy(&data));
                            }
                        }
                        Some(russh::ChannelMsg::ExitStatus { exit_status }) => {
                            exit_code = exit_status as i32;
                        }
                        Some(russh::ChannelMsg::ExitSignal {
                            signal_name,
                            error_message,
                            ..
                        }) => {
                            stderr.push_str(&error_message);
                            // По POSIX процесс, убитый сигналом N, возвращает 128 + N
                            exit_code = 128 + signal_number(&signal_name);
                        }
                        Some(russh::ChannelMsg::Close) | None => break,
                        _ => {}
                    }
                }
            }
        }

        let _ = channel.close().await;

        if was_cancelled {
            Err(SynexError::Cancelled)
        } else {
            Ok(SshOutput {
                stdout,
                stderr,
                exit_code,
            })
        }
    }
    pub async fn close(&mut self) -> Result<()> {
        self.session
            .disconnect(
                russh::Disconnect::ByApplication,
                "Closed by Synex IPC",
                "en",
            )
            .await
            .map_err(SynexError::Ssh)?;
        Ok(())
    }

    // Открываем канал для работы с SFTP
    pub async fn open_sftp_channel(&mut self) -> Result<russh::Channel<client::Msg>> {
        let channel = self.session.channel_open_session().await?;
        if let Err(err) = channel.request_subsystem(true, "sftp").await {
            let _ = channel.close().await;
            return Err(SynexError::Sftp(
                SftpErrorKind::Unavailable,
                format!("server refused SFTP subsystem: {}", err),
            ));
        }
        Ok(channel)
    }

    pub async fn close_sftp_channel(&mut self, channel: russh::Channel<client::Msg>) -> Result<()> {
        channel.close().await.map_err(SynexError::Ssh)?;
        Ok(())
    }
}

fn signal_number(sig: &russh::Sig) -> i32 {
    use russh::Sig;
    match sig {
        Sig::HUP => 1,
        Sig::INT => 2,
        Sig::QUIT => 3,
        Sig::ILL => 4,
        Sig::ABRT => 6,
        Sig::FPE => 8,
        Sig::KILL => 9,
        Sig::USR1 => 10,
        Sig::SEGV => 11,
        Sig::PIPE => 13,
        Sig::ALRM => 14,
        Sig::TERM => 15,
        // Неизвестный сигнал: номер не знаем, отдаём 0, тогда код будет 128
        Sig::Custom(_) => 0,
    }
}

// Ждём до deadline, если он задан; иначе никогда не завершаемся (select просто не выберет эту ветку).
async fn sleep_until_opt(deadline: Option<tokio::time::Instant>) {
    match deadline {
        Some(instant) => tokio::time::sleep_until(instant).await,
        None => std::future::pending().await,
    }
}
