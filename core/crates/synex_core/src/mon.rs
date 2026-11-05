use crate::{SshOutput, errors::SynexError, ssh::SshSession};

pub struct SshMonitoring {
    session: SshSession,
}

impl SshMonitoring {
    pub async fn Monitoring_req(&mut self) -> Result<SshOutput, SynexError> {
        let answer = self.session.execute_command(&"head -n 1 /proc/stat; sleep 0.2; head -n 1 /proc/stat; grep -E 'MemTotal|MemAvailable' /proc/meminfo".to_string()).await;
        return answer;
    }
}
