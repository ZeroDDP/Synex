// use crate::{SshOutput, errors::SynexError, ssh::SshSession};
// use russh::client::SftpSession;
// use tokio::fs::File;

// struct Sftp {
//     session: SshSession,
// }

// impl Sftp {
//     fn new(session: SshSession) -> Self {
//         Self { session }
//     }

//     async fn list_directory(&self, path: &str) -> Result<Vec<String>, SynexError> {
//         let quoted_path = format!("'{}'", path.replace('\'', "'\"'\"'"));
//         let output = self
//             .session
//             .execute_command(&format!("LC_ALL=C ls -la -- {}", quoted_path))
//             .await?;

//         Ok(output
//             .lines()
//             .filter(|line| !line.is_empty() && !line.starts_with("total "))
//             .map(str::to_string)
//             .collect())
//     }

//     async fn upload_file(&mut self, local_path: &str, remote_path: &str) -> Result<(), SynexError> {
//         // Сначала открываем локальный файл, затем канал SFTP.
//         let mut local_file = tokio::fs::File::open(local_path)
//             .await
//             .map_err(|err| SynexError::Internal(format!("Failed to open local file: {}", err)))?;

//         let channel = self.session.open_sftp_channel().await?;
//         let sftp = russh_sftp::client::SftpSession::new(channel.into_stream())
//             .await
//             .map_err(|err| SynexError::Internal(format!("Failed to initialize SFTP: {}", err)))?;

//         // Передаем файл потоком, не загружая его целиком в память.
//         let transfer_result = async {
//             let mut remote_file = sftp.create(remote_path).await.map_err(|err| {
//                 SynexError::Internal(format!("Failed to create remote file: {}", err))
//             })?;

//             tokio::io::copy(&mut local_file, &mut remote_file)
//                 .await
//                 .map_err(|err| SynexError::Internal(format!("Failed to upload file: {}", err)))?;

//             tokio::io::AsyncWriteExt::shutdown(&mut remote_file)
//                 .await
//                 .map_err(|err| {
//                     SynexError::Internal(format!("Failed to close remote file: {}", err))
//                 })?;

//             Ok::<(), SynexError>(())
//         }
//         .await;

//         // Закрываем SFTP и при ошибке передачи, сохраняя исходную ошибку.
//         let close_result = sftp
//             .close()
//             .await
//             .map_err(|err| SynexError::Internal(format!("Failed to close SFTP session: {}", err)));

//         transfer_result?;
//         close_result?;
//         Ok(())
//     }

//     async fn download_file(
//         &mut self,
//         remote_path: &str,
//         local_path: &str,
//     ) -> Result<(), SynexError> {
//         let channel = self.session.open_sftp_channel().await?;
//         let sftp = russh_sftp::client::SftpSession::new(channel.into_stream())
//             .await
//             .map_err(|err| SynexError::Internal(format!("Failed to initialize SFTP: {}", err)))?;

//         let transfer_result = async {
//             // Проверяем удаленный файл до создания локального.
//             let mut remote_file = sftp.open(remote_path).await.map_err(|err| {
//                 SynexError::Internal(format!("Failed to open remote file: {}", err))
//             })?;
//             let mut local_file = tokio::fs::File::create(local_path).await.map_err(|err| {
//                 SynexError::Internal(format!("Failed to create local file: {}", err))
//             })?;

//             tokio::io::copy(&mut remote_file, &mut local_file)
//                 .await
//                 .map_err(|err| SynexError::Internal(format!("Failed to download file: {}", err)))?;

//             tokio::io::AsyncWriteExt::flush(&mut local_file)
//                 .await
//                 .map_err(|err| {
//                     SynexError::Internal(format!("Failed to flush local file: {}", err))
//                 })?;

//             Ok::<(), SynexError>(())
//         }
//         .await;

//         // Закрываем SFTP независимо от результата передачи.
//         let close_result = sftp
//             .close()
//             .await
//             .map_err(|err| SynexError::Internal(format!("Failed to close SFTP session: {}", err)));

//         transfer_result?;
//         close_result?;
//         Ok(())
//     }

//     fn remove_file(&self, remote_path: &str) -> Result<(), SynexError> {
//         //TODO
//         unimplemented!()
//     }

//     fn remove_directory(&self, remote_path: &str) -> Result<(), SynexError> {
//         //TODO
//         unimplemented!()
//     }

//     fn create_directory(&self, remote_path: &str) -> Result<(), SynexError> {
//         //TODO
//         unimplemented!()
//     }

//     fn move_file(&self, source_path: &str, destination_path: &str) -> Result<(), SynexError> {
//         //TODO
//         unimplemented!()
//     }

//     fn copy_file(&self, source_path: &str, destination_path: &str) -> Result<(), SynexError> {
//         //TODO
//         unimplemented!()
//     }
// }
