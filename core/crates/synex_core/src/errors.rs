use thiserror::Error;

use crate::ssh::HostKey;

// Коды SFTP-ошибок. В ответе воркера они стоят в начале stderr, например
// "sftp_not_found: ...", и backend по ним различает причину.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SftpErrorKind {
    Unavailable,
    NotFound,
    PermissionDenied,
    Exists,
    NotADirectory,
    Failed,
}

impl SftpErrorKind {
    pub fn code(self) -> &'static str {
        match self {
            SftpErrorKind::Unavailable => "sftp_unavailable",
            SftpErrorKind::NotFound => "sftp_not_found",
            SftpErrorKind::PermissionDenied => "sftp_permission_denied",
            SftpErrorKind::Exists => "sftp_exists",
            SftpErrorKind::NotADirectory => "sftp_not_a_directory",
            SftpErrorKind::Failed => "sftp_failed",
        }
    }
}

#[derive(Error, Debug)]
pub enum SynexError {
    // 1. Универсальная ошибка для всего, что возвращает russh::Error
    // (подключение, открытие канала, exec, аутентификация).
    // #[from] позволяет использовать оператор ? без map_err.
    #[error("SSH protocol error: {0}")]
    Ssh(#[from] russh::Error),

    // 2. Ошибка загрузки ключа с диска.
    #[error("Failed to load SSH key: {0}")]
    SshKey(#[from] russh::keys::Error),

    // 3. Ошибка аутентификации (сервер сказал "нет").
    #[error("SSH authentication failed: access denied")]
    SshAuthFailed,

    // 4. Обычная ошибка ввода-вывода (чтение файла, сокеты).
    #[error("IO error: {0}")]
    Io(#[from] std::io::Error),

    // 5. Запасная ошибка для любых других текстовых сообщений.
    #[error("SSH internal error: {0}")]
    Internal(String),

    // 6. Сервер не известен: отпечаток не передан, backend должен подтвердить его у пользователя.
    #[error("host_key_unknown: unknown host key {}", .0.fingerprint)]
    HostKeyUnknown(HostKey),

    // 7. Отпечаток сервера не совпал с доверенным. Подключение не продолжаем.
    #[error("host_key_mismatch: expected {expected}, got {}", actual.fingerprint)]
    HostKeyMismatch { expected: String, actual: HostKey },

    // 8. Ошибка SFTP с кодом из SftpErrorKind.
    #[error("{}: {}", .0.code(), .1)]
    Sftp(SftpErrorKind, String),

    // 9. Операцию остановили через действие cancel: это не сбой, а штатная отмена.
    #[error("cancelled")]
    Cancelled,

    // 10. Ошибка Docker. Первый элемент — код из docker_* (он же в начале stderr).
    #[error("{0}: {1}")]
    Docker(&'static str, String),
}

pub type Result<T> = std::result::Result<T, SynexError>;
