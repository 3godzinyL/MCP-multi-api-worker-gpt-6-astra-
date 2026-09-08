use anyhow::{bail, Result};
use zeroize::Zeroizing;

/// Prepare the private gateway credential without an interactive token form.
/// The token never enters source files; children inherit it from this process.
pub fn ensure_local_token(config: &std::path::Path) -> Result<String> {
    let settings = super::load_settings(config)?;
    let token = match get_secret("local-proxy-token", &settings.proxy_token_env)? {
        Some(token) => token,
        None => {
            let token = format!(
                "{}{}",
                uuid::Uuid::new_v4().simple(),
                uuid::Uuid::new_v4().simple()
            );
            #[cfg(windows)]
            store_local_token(&token)?;
            token
        }
    };
    std::env::set_var(&settings.proxy_token_env, &token);
    Ok(token)
}

#[cfg(windows)]
fn store_local_token(token: &str) -> Result<()> {
    use windows_sys::Win32::Security::Credentials::{
        CredWriteW, CREDENTIALW, CRED_PERSIST_LOCAL_MACHINE, CRED_TYPE_GENERIC,
    };
    let mut target: Vec<u16> = "local-proxy-token@CodexLocalResponsesProxy"
        .encode_utf16()
        .chain(Some(0))
        .collect();
    let mut username: Vec<u16> = "local-proxy-token".encode_utf16().chain(Some(0)).collect();
    let mut secret = Zeroizing::new(token.encode_utf16().collect::<Vec<_>>());
    // SAFETY: all pointers refer to live, appropriately sized buffers for the
    // duration of CredWriteW. Windows copies the blob into the user's vault.
    let record = CREDENTIALW {
        Type: CRED_TYPE_GENERIC,
        TargetName: target.as_mut_ptr(),
        CredentialBlobSize: (secret.len() * 2) as u32,
        CredentialBlob: secret.as_mut_ptr().cast(),
        Persist: CRED_PERSIST_LOCAL_MACHINE,
        UserName: username.as_mut_ptr(),
        ..unsafe { std::mem::zeroed() }
    };
    if unsafe { CredWriteW(&record, 0) } == 0 {
        bail!("Cannot prepare the local token in Windows Credential Manager");
    }
    Ok(())
}

/// Reads environment first, then the existing Python-keyring Windows vault entry.
/// No plaintext fallback and no vault writes are performed by this function.
pub fn get_secret(name: &str, env_name: &str) -> Result<Option<String>> {
    if !env_name.is_empty() {
        if let Ok(value) = std::env::var(env_name) {
            if !value.trim().is_empty() {
                return validate(value);
            }
        }
    }
    #[cfg(windows)]
    {
        windows_secret(name)
    }
    #[cfg(not(windows))]
    {
        let _ = name;
        Ok(None)
    }
}

fn validate(value: String) -> Result<Option<String>> {
    let value = Zeroizing::new(value);
    let secret = value.trim();
    if secret.is_empty() {
        return Ok(None);
    }
    if secret.len() > 16384 || !secret.is_ascii() || secret.bytes().any(|b| b < 0x21 || b == 0x7f) {
        bail!("Credential must be a nonempty ASCII token without whitespace");
    }
    Ok(Some(secret.into()))
}

#[cfg(windows)]
fn windows_secret(name: &str) -> Result<Option<String>> {
    use windows_sys::Win32::{
        Foundation::{GetLastError, ERROR_NOT_FOUND},
        Security::Credentials::{CredFree, CredReadW, CREDENTIALW, CRED_TYPE_GENERIC},
    };
    use zeroize::Zeroize;
    const SERVICE: &str = "CodexLocalResponsesProxy";

    struct Credential(*mut CREDENTIALW);
    impl Drop for Credential {
        fn drop(&mut self) {
            // SAFETY: CredReadW allocated this buffer; it is freed exactly once.
            unsafe {
                let record = &mut *self.0;
                if !record.CredentialBlob.is_null() && record.CredentialBlobSize <= 32768 {
                    std::slice::from_raw_parts_mut(
                        record.CredentialBlob,
                        record.CredentialBlobSize as usize,
                    )
                    .zeroize();
                }
                CredFree(self.0.cast());
            }
        }
    }
    fn read(target: &str) -> Result<Option<Credential>> {
        let mut target: Vec<u16> = target.encode_utf16().chain(Some(0)).collect();
        let mut pointer = std::ptr::null_mut();
        // SAFETY: target is a terminated UTF-16 string and pointer is a valid out parameter.
        let ok = unsafe { CredReadW(target.as_mut_ptr(), CRED_TYPE_GENERIC, 0, &mut pointer) };
        if ok == 0 {
            if unsafe { GetLastError() } == ERROR_NOT_FOUND {
                return Ok(None);
            }
            bail!("Cannot read Windows Credential Manager");
        }
        if pointer.is_null() {
            bail!("Invalid Windows credential record");
        }
        Ok(Some(Credential(pointer)))
    }
    fn username(credential: &Credential) -> String {
        // SAFETY: Windows provides a valid terminated username inside the owned credential.
        unsafe {
            let pointer = (*credential.0).UserName;
            if pointer.is_null() {
                return String::new();
            }
            let mut len = 0;
            while len < 513 && *pointer.add(len) != 0 {
                len += 1;
            }
            String::from_utf16_lossy(std::slice::from_raw_parts(pointer, len))
        }
    }
    fn decode(credential: Credential) -> Result<Option<String>> {
        // SAFETY: CredentialBlobSize is the buffer length returned by Windows.
        let record = unsafe { &*credential.0 };
        let length = record.CredentialBlobSize as usize;
        if length == 0 {
            return Ok(None);
        }
        if length > 32768 || record.CredentialBlob.is_null() {
            bail!("Invalid Windows credential size");
        }
        let bytes = unsafe { std::slice::from_raw_parts(record.CredentialBlob, length) };
        let value = if length % 2 == 0 {
            let mut units: Vec<u16> = bytes
                .chunks_exact(2)
                .map(|pair| u16::from_le_bytes([pair[0], pair[1]]))
                .collect();
            let decoded = String::from_utf16(&units);
            units.zeroize();
            decoded
                .or_else(|_| String::from_utf8(bytes.to_vec()).map_err(|_| ()))
                .map_err(|_| anyhow::anyhow!("Invalid Windows credential encoding"))?
        } else {
            String::from_utf8(bytes.to_vec())
                .map_err(|_| anyhow::anyhow!("Invalid Windows credential encoding"))?
        };
        validate(value)
    }
    if let Some(credential) = read(SERVICE)? {
        if username(&credential) == name {
            return decode(credential);
        }
    }
    match read(&format!("{name}@{SERVICE}"))? {
        Some(credential) if username(&credential) == name => decode(credential),
        _ => Ok(None),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn header_injection_and_non_ascii_are_rejected() {
        for value in ["bad\r\nHeader: value", "bad token", "zażółć", "bad\u{7f}"] {
            assert!(validate(value.into()).is_err());
        }
        assert_eq!(
            validate("good-token_123".into()).unwrap().as_deref(),
            Some("good-token_123")
        );
    }
}
