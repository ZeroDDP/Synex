use crate::errors::{Result, SftpErrorKind, SynexError};
use crate::ssh::SshSession;
use russh_sftp::client::SftpSession;
use russh_sftp::client::error::Error as SftpClientError;
use russh_sftp::protocol::{FileAttributes, StatusCode};
use serde::Serialize;
use std::path::Path;
use tokio::io::AsyncWriteExt;

pub struct Sftp<'a> {
    session: &'a mut SshSession,
}

#[derive(Serialize, Debug)]
pub struct DirEntryInfo {
    #[serde(rename = "Type")]
    pub entry_type: String,
    pub name: String,
    pub size: u64,
    pub rights: String,
    pub mtime: Option<u32>,
    pub uid: Option<u32>,
    pub gid: Option<u32>,
    pub link_target: Option<String>,
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

fn join_remote(dir: &str, name: &str) -> String {
    format!("{}/{}", dir.trim_end_matches('/'), name)
}

// Переводим статус SFTP-сервера в код ошибки. Остальное считаем общей ошибкой.
fn sftp_kind(err: &SftpClientError) -> SftpErrorKind {
    match err {
        SftpClientError::Status(status) => match status.status_code {
            StatusCode::NoSuchFile => SftpErrorKind::NotFound,
            StatusCode::PermissionDenied => SftpErrorKind::PermissionDenied,
            _ => SftpErrorKind::Failed,
        },
        _ => SftpErrorKind::Failed,
    }
}

fn sftp_error(context: &str, err: SftpClientError) -> SynexError {
    SynexError::Sftp(sftp_kind(&err), format!("{}: {}", context, err))
}

async fn close_sftp(sftp: SftpSession) -> Result<()> {
    sftp.close().await.map_err(|err| {
        SynexError::Sftp(
            SftpErrorKind::Failed,
            format!("Failed to close SFTP session: {}", err),
        )
    })
}

fn entry_info(name: String, attrs: &FileAttributes, link_target: Option<String>) -> DirEntryInfo {
    let entry_type = if attrs.is_dir() {
        "folder"
    } else if attrs.is_symlink() {
        "symlink"
    } else {
        "file"
    };
    DirEntryInfo {
        entry_type: entry_type.to_string(),
        name,
        size: attrs.size.unwrap_or(0),
        rights: format_rights(attrs.permissions.unwrap_or(0)),
        mtime: attrs.mtime,
        uid: attrs.uid,
        gid: attrs.gid,
        link_target,
    }
}

impl<'a> Sftp<'a> {
    pub fn new(session: &'a mut SshSession) -> Self {
        Self { session }
    }

    // Открываем SFTP-канал. Если сервер отказал в подсистеме или не ответил, это sftp_unavailable.
    async fn open_sftp(&mut self) -> Result<SftpSession> {
        let channel = self.session.open_sftp_channel().await?;
        SftpSession::new(channel.into_stream())
            .await
            .map_err(|err| {
                SynexError::Sftp(
                    SftpErrorKind::Unavailable,
                    format!("failed to initialize SFTP: {}", err),
                )
            })
    }

    pub async fn list_directory(&mut self, path: &str) -> Result<Vec<String>> {
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

    pub async fn list_directory_entries(&mut self, path: &str) -> Result<Vec<DirEntryInfo>> {
        let sftp = self.open_sftp().await?;

        let list_result = async {
            let dir_attrs = sftp
                .metadata(path)
                .await
                .map_err(|err| sftp_error("Failed to read remote directory", err))?;
            if !dir_attrs.is_dir() {
                return Err(SynexError::Sftp(
                    SftpErrorKind::NotADirectory,
                    format!("not a directory: {}", path),
                ));
            }

            let dir = sftp
                .read_dir(path)
                .await
                .map_err(|err| sftp_error("Failed to read remote directory", err))?;

            let mut entries = Vec::new();
            for entry in dir {
                let name = entry.file_name();
                if name == "." || name == ".." {
                    continue;
                }
                let attrs = entry.metadata();
                let link_target = if attrs.is_symlink() {
                    sftp.read_link(join_remote(path, &name)).await.ok()
                } else {
                    None
                };
                entries.push(entry_info(name, &attrs, link_target));
            }

            Ok::<Vec<DirEntryInfo>, SynexError>(entries)
        }
        .await;

        let close_result = close_sftp(sftp).await;

        let entries = list_result?;
        close_result?;
        Ok(entries)
    }

    // Информация об одном пути. Симлинк не переходим, возвращаем сам симлинк.
    pub async fn stat(&mut self, path: &str) -> Result<DirEntryInfo> {
        let sftp = self.open_sftp().await?;

        let stat_result = async {
            let attrs = sftp
                .symlink_metadata(path)
                .await
                .map_err(|err| sftp_error("Failed to stat remote path", err))?;
            let link_target = if attrs.is_symlink() {
                sftp.read_link(path).await.ok()
            } else {
                None
            };
            let name = file_name_of(path).unwrap_or(path).to_string();

            Ok::<DirEntryInfo, SynexError>(entry_info(name, &attrs, link_target))
        }
        .await;

        let close_result = close_sftp(sftp).await;

        let info = stat_result?;
        close_result?;
        Ok(info)
    }

    pub async fn upload_file(&mut self, local_path: &str, remote_path: &str) -> Result<()> {
        // Сначала открываем локальный файл, затем канал SFTP.
        let mut local_file = tokio::fs::File::open(local_path)
            .await
            .map_err(|err| SynexError::Internal(format!("Failed to open local file: {}", err)))?;

        let sftp = self.open_sftp().await?;

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
                join_remote(remote_path, name)
            } else {
                remote_path.to_string()
            };

            let mut remote_file = sftp
                .create(remote_target)
                .await
                .map_err(|err| sftp_error("Failed to create remote file", err))?;

            tokio::io::copy(&mut local_file, &mut remote_file)
                .await
                .map_err(|err| {
                    SynexError::Sftp(
                        SftpErrorKind::Failed,
                        format!("Failed to upload file: {}", err),
                    )
                })?;

            remote_file.shutdown().await.map_err(|err| {
                SynexError::Sftp(
                    SftpErrorKind::Failed,
                    format!("Failed to close remote file: {}", err),
                )
            })?;

            Ok::<(), SynexError>(())
        }
        .await;

        // Закрываем SFTP и при ошибке передачи, сохраняя исходную ошибку.
        let close_result = close_sftp(sftp).await;

        transfer_result?;
        close_result?;
        Ok(())
    }

    pub async fn download_file(&mut self, remote_path: &str, local_path: &str) -> Result<()> {
        let sftp = self.open_sftp().await?;

        let transfer_result = async {
            // Проверяем удаленный файл до создания локального.
            let mut remote_file = sftp
                .open(remote_path)
                .await
                .map_err(|err| sftp_error("Failed to open remote file", err))?;

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
                .map_err(|err| {
                    SynexError::Sftp(
                        SftpErrorKind::Failed,
                        format!("Failed to download file: {}", err),
                    )
                })?;

            local_file.flush().await.map_err(|err| {
                SynexError::Internal(format!("Failed to flush local file: {}", err))
            })?;

            Ok::<(), SynexError>(())
        }
        .await;

        // Закрываем SFTP независимо от результата передачи.
        let close_result = close_sftp(sftp).await;

        transfer_result?;
        close_result?;
        Ok(())
    }

    pub async fn remove_file(&mut self, remote_path: &str) -> Result<()> {
        let sftp = self.open_sftp().await?;

        let remove_result = sftp
            .remove_file(remote_path)
            .await
            .map_err(|err| sftp_error("Failed to remove remote file", err));

        // Закрываем SFTP независимо от результата удаления.
        let close_result = close_sftp(sftp).await;

        remove_result?;
        close_result?;
        Ok(())
    }

    pub async fn remove_directory(&mut self, remote_path: &str) -> Result<()> {
        let sftp = self.open_sftp().await?;

        let remove_result = sftp
            .remove_dir(remote_path)
            .await
            .map_err(|err| sftp_error("Failed to remove remote directory", err));

        // Закрываем SFTP независимо от результата удаления.
        let close_result = close_sftp(sftp).await;

        remove_result?;
        close_result?;
        Ok(())
    }

    pub async fn create_directory(&mut self, remote_path: &str) -> Result<()> {
        let sftp = self.open_sftp().await?;

        let create_result = async {
            match sftp.symlink_metadata(remote_path).await {
                Ok(_) => {
                    return Err(SynexError::Sftp(
                        SftpErrorKind::Exists,
                        format!("remote path already exists: {}", remote_path),
                    ));
                }
                Err(err) if sftp_kind(&err) != SftpErrorKind::NotFound => {
                    return Err(sftp_error("Failed to check remote path", err));
                }
                Err(_) => {}
            }

            sftp.create_dir(remote_path)
                .await
                .map_err(|err| sftp_error("Failed to create remote directory", err))
        }
        .await;

        // Закрываем SFTP независимо от результата создания.
        let close_result = close_sftp(sftp).await;

        create_result?;
        close_result?;
        Ok(())
    }

    pub async fn move_file(&mut self, source_path: &str, destination_path: &str) -> Result<()> {
        self.move_path(source_path, destination_path, None).await
    }

    // Перемещение папки. Источник должен быть папкой; симлинк на папку отклоняем (lstat).
    pub async fn move_dir(&mut self, source_path: &str, destination_path: &str) -> Result<()> {
        self.move_path(source_path, destination_path, Some(true))
            .await
    }

    async fn move_path(
        &mut self,
        source_path: &str,
        destination_path: &str,
        expect_dir: Option<bool>,
    ) -> Result<()> {
        let sftp = self.open_sftp().await?;

        let rename_result = async {
            let source_attrs = sftp
                .symlink_metadata(source_path)
                .await
                .map_err(|err| sftp_error("Failed to access source", err))?;

            if let Some(want_dir) = expect_dir {
                if source_attrs.is_dir() != want_dir {
                    return Err(SynexError::Sftp(
                        SftpErrorKind::NotADirectory,
                        format!("source is not a directory: {}", source_path),
                    ));
                }
            }

            match sftp.symlink_metadata(destination_path).await {
                Ok(_) => {
                    return Err(SynexError::Sftp(
                        SftpErrorKind::Exists,
                        format!("destination already exists: {}", destination_path),
                    ));
                }
                Err(err) if sftp_kind(&err) != SftpErrorKind::NotFound => {
                    return Err(sftp_error("Failed to check destination", err));
                }
                Err(_) => {}
            }

            sftp.rename(source_path, destination_path)
                .await
                .map_err(|err| sftp_error("Failed to move remote file", err))
        }
        .await;

        // Закрываем SFTP независимо от результата перемещения.
        let close_result = close_sftp(sftp).await;

        rename_result?;
        close_result?;
        Ok(())
    }

    pub async fn copy_file(&mut self, source_path: &str, destination_path: &str) -> Result<()> {
        // SFTP не поддерживает копирование напрямую, поэтому выполняем на сервере через cp.
        let quoted_source = format!("'{}'", source_path.replace('\'', "'\"'\"'"));
        let quoted_destination = format!("'{}'", destination_path.replace('\'', "'\"'\"'"));
        let output = self
            .session
            .execute_command(&format!("cp -- {} {}", quoted_source, quoted_destination))
            .await?;

        if output.exit_code != 0 {
            return Err(SynexError::Sftp(
                SftpErrorKind::Failed,
                format!("Failed to copy remote file: {}", output.stderr.trim()),
            ));
        }
        Ok(())
    }
}
