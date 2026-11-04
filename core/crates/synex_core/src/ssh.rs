// use russh::client::AuthResult;
use russh::keys::PrivateKeyWithHashAlg;
use russh::{
    client::{self, Handler},
    keys::PublicKeyOrCertificate,
};
use serde::{Deserialize, Serialize};
use std::sync::Arc;

use crate::errors::{Result, SynexError}; // файл ошибок

#[derive(Debug, Serialize, Deserialize)]
pub struct SshOutput {
    pub stdout: String,
    pub stderr: String,
    pub exit_code: i32,
}

// Хендлер отвечает на вопросы сервера.
struct ClientHandler;

impl Handler for ClientHandler {
    type Error = russh::Error;

    // Для прототипа просто доверяем любому ключу сервера.
    // (В продакшене тут сверяют fingerprint с known_hosts)
    async fn check_server_key(
        &mut self,
        _server_public_key: &PublicKeyOrCertificate,
    ) -> std::result::Result<bool, Self::Error> {
        Ok(true)
    }
}

pub struct SshSession {
    session: client::Handle<ClientHandler>,
    pub prev_cpu: Option<(u64, u64)>, // idle, total
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
    ) -> Result<Self> {
        // println!(" Загружаем ключ: {}", key_path);
        let config = Arc::new(client::Config::default());

        let mut session = client::connect(config, (host, port), ClientHandler)
            .await
            .map_err(SynexError::Ssh)?;

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

        // Проверяем результат
        // if !auth_result.success() {
        //     return Err(SynexError::SshAuthFailed);
        // }

        // let key_pair = russh::keys::load_secret_key(&key_path, None)?;

        // println!("🌐 Подключаемся к {}:{}...", host, port);

        // println!("🔐 Аутентификация...");
        // let auth_success: AuthResult = session
        //     .authenticate_publickey(
        //         username,
        //         PrivateKeyWithHashAlg::new(
        //             Arc::new(key_pair),
        //             session.best_supported_rsa_hash().await?.flatten(),
        //         ),
        //     )
        //     .await?;

        if !auth_result.success() {
            return Err(SynexError::SshAuthFailed);
        }

        // println!("✅ Подключение установлено");
        Ok(SshSession {
            session,
            prev_cpu: None,
        })
    }

    // exec in session using
    pub async fn execute_command(&mut self, command: &str) -> Result<SshOutput> {
        let mut channel = self.session.channel_open_session().await?;
        channel.exec(true, command).await?;

        let mut stdout = String::new();
        let mut stderr = String::new();
        let mut exit_code = 0;

        while let Some(msg) = channel.wait().await {
            match msg {
                russh::ChannelMsg::Data { data } => {
                    stdout.push_str(&String::from_utf8_lossy(&data));
                }
                russh::ChannelMsg::ExtendedData { data, ext } => {
                    if ext == 1 {
                        stderr.push_str(&String::from_utf8_lossy(&data));
                    }
                }
                russh::ChannelMsg::ExitStatus { exit_status } => {
                    exit_code = exit_status as i32;
                }
                russh::ChannelMsg::ExitSignal { error_message, .. } => {
                    stderr.push_str(&error_message);
                    exit_code = -1;
                }
                russh::ChannelMsg::Eof | russh::ChannelMsg::Close => {
                    // Сервер закрыл поток данных, выходим без порчи exit_code
                    break;
                }
                _ => {}
            }
        }

        Ok(SshOutput {
            stdout,
            stderr,
            exit_code,
        })
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
        channel.request_subsystem(true, "sftp").await?;
        Ok(channel)
    }

    pub async fn close_sftp_channel(&mut self, channel: russh::Channel<client::Msg>) -> Result<()> {
        channel.close().await.map_err(SynexError::Ssh)?;
        Ok(())
    }
}
