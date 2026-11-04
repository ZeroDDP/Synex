use thiserror::Error;

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
}

pub type Result<T> = std::result::Result<T, SynexError>;
