use crate::{SshOutput, errors::SynexError, ssh::SshSession};
use russh_sftp::client::SftpSession;
use russh_sftp::protocol::FileAttributes;
use serde::Serialize;
use std::path::Path;
use std::sync::MutexGuard;
use tokio::fs::File;

pub struct Sftp<'a> {
    session: &'a mut SshSession,
}

#[derive(Serialize, Debug)]
pub struct DirEntryInfo {
    #[serde(rename = "Type")]
    pub entry_type: String,
    pub name: String,
    pub weight: u64,
    pub rights: String,
}

fn format_rights(mode: u32) -> String {
    const FLAGS: [(u32, char); 9] = [
        (0o400, 'r'),
        (0o200, 'w'),
        (0o100, 'x'),
        (0o040, 'r'),
        (0o020, 'w'),
        (0o010, 'x'),
        (0o004, 'r'),
        (0o002, 'w'),
        (0o001, 'x'),
    ];
    FLAGS
        .iter()
        .map(|(bit, c)| if mode & bit != 0 { *c } else { '-' })
        .collect()
}

fn file_name_of(path: &str) -> Option<&str> {
    path.trim_end_matches(['/', '\\'])
        .rsplit(['/', '\\'])
        .next()
        .filter(|name| !name.is_empty())
}

impl<'a> Sftp<'a> {
    pub fn new(session: &'a mut SshSession) -> Self {
        Self { session }
    }

    pub async fn list_directory(&mut self, path: &str) -> Result<Vec<String>, SynexError> {
        let quoted_path = format!("'{}'", path.replace('\'', "'\"'\"'"));
        let output = self
            .session
            .execute_command(&format!("LC_ALL=C ls -la -- {}", quoted_path))
            .await?;

        Ok(output
            .stdout
            .lines()
            .filter(|line| !line.is_empty() && !line.starts_with("total "))
            .map(str::to_string)
            .collect())
    }

    pub async fn list_directory_entries(
        &mut self,
        path: &str,
    ) -> Result<Vec<DirEntryInfo>, SynexError> {
        let channel = self.session.open_sftp_channel().await?;
        let sftp = russh_sftp::client::SftpSession::new(channel.into_stream())
            .await
            .map_err(|err| SynexError::Internal(format!("Failed to initialize SFTP: {}", err)))?;

        let list_result = async {
            let dir = sftp.read_dir(path).await.map_err(|err| {
                SynexError::Internal(format!("Failed to read remote directory: {}", err))
            })?;

            let mut entries = Vec::new();
            for entry in dir {
                let name = entry.file_name();
                if name == "." || name == ".." {
                    continue;
                }
                let attrs = entry.metadata();
                let entry_type = if attrs.is_dir() {
                    "folder"
                } else if attrs.is_symlink() {
                    "symlink"
                } else {
                    "file"
                };
                entries.push(DirEntryInfo {
                    entry_type: entry_type.to_string(),
                    name,
                    weight: attrs.size.unwrap_or(0),
                    rights: format_rights(attrs.permissions.unwrap_or(0)),
                });
            }

            Ok::<Vec<DirEntryInfo>, SynexError>(entries)
        }
        .await;

        let close_result = sftp
            .close()
            .await
            .map_err(|err| SynexError::Internal(format!("Failed to close SFTP session: {}", err)));

        let entries = list_result?;
        close_result?;
        Ok(entries)
    }

    pub async fn upload_file(
        &mut self,
        local_path: &str,
        remote_path: &str,
    ) -> Result<(), SynexError> {
        // Сначала открываем локальный файл, затем канал SFTP.
        let mut local_file = tokio::fs::File::open(local_path)
            .await
            .map_err(|err| SynexError::Internal(format!("Failed to open local file: {}", err)))?;

        let channel = self.session.open_sftp_channel().await?;
        let sftp = russh_sftp::client::SftpSession::new(channel.into_stream())
            .await
            .map_err(|err| SynexError::Internal(format!("Failed to initialize SFTP: {}", err)))?;

        // Передаем файл потоком, не загружая его целиком в память.
        let transfer_result = async {
            // Если назначение — папка, кладем файл в нее под его исходным именем.
            let remote_target = if remote_path.ends_with('/')
                || sftp
                    .metadata(remote_path)
                    .await
                    .map(|attrs| attrs.is_dir())
                    .unwrap_or(false)
            {
                let name = file_name_of(local_path).ok_or_else(|| {
                    SynexError::Internal(format!("Invalid local file path: {}", local_path))
                })?;
                format!("{}/{}", remote_path.trim_end_matches('/'), name)
            } else {
                remote_path.to_string()
            };

            let mut remote_file = sftp.create(remote_target).await.map_err(|err| {
                SynexError::Internal(format!("Failed to create remote file: {}", err))
            })?;

            tokio::io::copy(&mut local_file, &mut remote_file)
                .await
                .map_err(|err| SynexError::Internal(format!("Failed to upload file: {}", err)))?;

            tokio::io::AsyncWriteExt::shutdown(&mut remote_file)
                .await
                .map_err(|err| {
                    SynexError::Internal(format!("Failed to close remote file: {}", err))
                })?;

            Ok::<(), SynexError>(())
        }
        .await;

        // Закрываем SFTP и при ошибке передачи, сохраняя исходную ошибку.
        let close_result = sftp
            .close()
            .await
            .map_err(|err| SynexError::Internal(format!("Failed to close SFTP session: {}", err)));

        transfer_result?;
        close_result?;
        Ok(())
    }

    pub async fn download_file(
        &mut self,
        remote_path: &str,
        local_path: &str,
    ) -> Result<(), SynexError> {
        let channel = self.session.open_sftp_channel().await?;
        let sftp = russh_sftp::client::SftpSession::new(channel.into_stream())
            .await
            .map_err(|err| SynexError::Internal(format!("Failed to initialize SFTP: {}", err)))?;

        let transfer_result = async {
            // Проверяем удаленный файл до создания локального.
            let mut remote_file = sftp.open(remote_path).await.map_err(|err| {
                SynexError::Internal(format!(
                    "Failed to open remote file '{}': {}",
                    remote_path, err
                ))
            })?;
            // Если назначение — папка, сохраняем файл в нее под его исходным именем.
            let local_target = if local_path.ends_with(['/', '\\'])
                || tokio::fs::metadata(local_path)
                    .await
                    .map(|meta| meta.is_dir())
                    .unwrap_or(false)
            {
                let name = file_name_of(remote_path).ok_or_else(|| {
                    SynexError::Internal(format!("Invalid remote file path: {}", remote_path))
                })?;
                Path::new(local_path)
                    .join(name)
                    .to_string_lossy()
                    .into_owned()
            } else {
                local_path.to_string()
            };

            let mut local_file = tokio::fs::File::create(local_target).await.map_err(|err| {
                SynexError::Internal(format!("Failed to create local file: {}", err))
            })?;

            tokio::io::copy(&mut remote_file, &mut local_file)
                .await
                .map_err(|err| SynexError::Internal(format!("Failed to download file: {}", err)))?;

            tokio::io::AsyncWriteExt::flush(&mut local_file)
                .await
                .map_err(|err| {
                    SynexError::Internal(format!("Failed to flush local file: {}", err))
                })?;

            Ok::<(), SynexError>(())
        }
        .await;

        // Закрываем SFTP независимо от результата передачи.
        let close_result = sftp
            .close()
            .await
            .map_err(|err| SynexError::Internal(format!("Failed to close SFTP session: {}", err)));

        transfer_result?;
        close_result?;
        Ok(())
    }

    pub async fn remove_file(&mut self, remote_path: &str) -> Result<(), SynexError> {
        let channel = self.session.open_sftp_channel().await?;
        let sftp = russh_sftp::client::SftpSession::new(channel.into_stream())
            .await
            .map_err(|err| SynexError::Internal(format!("Failed to initialize SFTP: {}", err)))?;

        let remove_result = sftp
            .remove_file(remote_path)
            .await
            .map_err(|err| SynexError::Internal(format!("Failed to remove remote file: {}", err)));

        // Закрываем SFTP независимо от результата удаления.
        let close_result = sftp
            .close()
            .await
            .map_err(|err| SynexError::Internal(format!("Failed to close SFTP session: {}", err)));

        remove_result?;
        close_result?;
        Ok(())
    }

    pub async fn remove_directory(&mut self, remote_path: &str) -> Result<(), SynexError> {
        let channel = self.session.open_sftp_channel().await?;
        let sftp = russh_sftp::client::SftpSession::new(channel.into_stream())
            .await
            .map_err(|err| SynexError::Internal(format!("Failed to initialize SFTP: {}", err)))?;

        let remove_result = sftp.remove_dir(remote_path).await.map_err(|err| {
            SynexError::Internal(format!("Failed to remove remote directory: {}", err))
        });

        // Закрываем SFTP независимо от результата удаления.
        let close_result = sftp
            .close()
            .await
            .map_err(|err| SynexError::Internal(format!("Failed to close SFTP session: {}", err)));

        remove_result?;
        close_result?;
        Ok(())
    }

    pub async fn create_directory(&mut self, remote_path: &str) -> Result<(), SynexError> {
        let channel = self.session.open_sftp_channel().await?;
        let sftp = russh_sftp::client::SftpSession::new(channel.into_stream())
            .await
            .map_err(|err| SynexError::Internal(format!("Failed to initialize SFTP: {}", err)))?;

        let create_result = sftp.create_dir(remote_path).await.map_err(|err| {
            SynexError::Internal(format!("Failed to create remote directory: {}", err))
        });

        // Закрываем SFTP независимо от результата создания.
        let close_result = sftp
            .close()
            .await
            .map_err(|err| SynexError::Internal(format!("Failed to close SFTP session: {}", err)));

        create_result?;
        close_result?;
        Ok(())
    }

    pub async fn move_file(
        &mut self,
        source_path: &str,
        destination_path: &str,
    ) -> Result<(), SynexError> {
        let channel = self.session.open_sftp_channel().await?;
        let sftp = russh_sftp::client::SftpSession::new(channel.into_stream())
            .await
            .map_err(|err| SynexError::Internal(format!("Failed to initialize SFTP: {}", err)))?;

        let rename_result = sftp
            .rename(source_path, destination_path)
            .await
            .map_err(|err| SynexError::Internal(format!("Failed to move remote file: {}", err)));

        // Закрываем SFTP независимо от результата перемещения.
        let close_result = sftp
            .close()
            .await
            .map_err(|err| SynexError::Internal(format!("Failed to close SFTP session: {}", err)));

        rename_result?;
        close_result?;
        Ok(())
    }

    pub async fn copy_file(
        &mut self,
        source_path: &str,
        destination_path: &str,
    ) -> Result<(), SynexError> {
        // SFTP не поддерживает копирование напрямую, поэтому выполняем на сервере через cp.
        let quoted_source = format!("'{}'", source_path.replace('\'', "'\"'\"'"));
        let quoted_destination = format!("'{}'", destination_path.replace('\'', "'\"'\"'"));
        let output = self
            .session
            .execute_command(&format!("cp -- {} {}", quoted_source, quoted_destination))
            .await?;

        if output.exit_code != 0 {
            return Err(SynexError::Internal(format!(
                "Failed to copy remote file: {}",
                output.stderr
            )));
        }
        Ok(())
    }
}
