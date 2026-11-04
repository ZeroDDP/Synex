// Объявляем модули. Каждый файл в src/ становится модулем.
pub mod errors;
pub mod mon;
pub mod sftp;
pub mod ssh;
// Реэкспорт типов, чтобы из воркера было удобнее импортировать.
// Вместо `use synex_core::ssh::SshOutput` можно будет писать `use synex_core::SshOutput`.
pub use ssh::SshOutput;
