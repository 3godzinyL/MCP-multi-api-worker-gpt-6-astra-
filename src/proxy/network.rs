use anyhow::{bail, Result};
use reqwest::dns::{Addrs, Name, Resolve, Resolving};
use std::{
    net::{IpAddr, Ipv4Addr, Ipv6Addr, SocketAddr},
    sync::Arc,
    time::Duration,
};
use url::{Host, Url};

pub(super) fn public_address(ip: IpAddr) -> bool {
    match ip {
        IpAddr::V4(ip) => public_v4(ip),
        IpAddr::V6(ip) => {
            if let Some(v4) = ip.to_ipv4_mapped() {
                return public_v4(v4);
            }
            let segments = ip.segments();
            // Only globally routable unicast 2000::/3; exclude documentation,
            // transition ranges that could smuggle private IPv4, and benchmarking.
            segments[0] & 0xe000 == 0x2000
                && !(segments[0] == 0x2001 && segments[1] <= 0x01ff)
                && !(segments[0] == 0x2001 && segments[1] == 0x0db8)
                && segments[0] != 0x2002
                && !(segments[0] == 0x3fff && segments[1] <= 0x0fff)
        }
    }
}

fn public_v4(ip: Ipv4Addr) -> bool {
    let [a, b, c, _] = ip.octets();
    !(a == 0
        || a == 10
        || a == 127
        || a >= 224
        || (a == 100 && (64..=127).contains(&b))
        || (a == 169 && b == 254)
        || ip == Ipv4Addr::new(168, 63, 129, 16)
        || (a == 172 && (16..=31).contains(&b))
        || (a == 192 && b == 168)
        || (a == 192 && b == 0 && c == 0)
        || (a == 192 && b == 0 && c == 2)
        || (a == 192 && b == 88 && c == 99)
        || (a == 198 && (b == 18 || b == 19))
        || (a == 198 && b == 51 && c == 100)
        || (a == 203 && b == 0 && c == 113))
}

pub(super) fn validate_url(value: &str, allow_loopback: bool) -> Result<Url> {
    let invalid = || {
        anyhow::anyhow!("Invalid upstream URL: require public HTTPS without credentials, query, fragment, or /responses suffix")
    };
    let url = Url::parse(value).map_err(|_| invalid())?;
    if !url.username().is_empty()
        || url.password().is_some()
        || url.query().is_some()
        || url.fragment().is_some()
        || url.path().trim_end_matches('/').ends_with("/responses")
        || !["https", "http"].contains(&url.scheme())
        || url.port_or_known_default().is_none()
    {
        return Err(invalid());
    }
    let local = match url.host().ok_or_else(invalid)? {
        Host::Ipv4(ip) => {
            if !ip.is_loopback() && !public_v4(ip) {
                return Err(invalid());
            }
            ip.is_loopback()
        }
        Host::Ipv6(ip) => {
            if !ip.is_loopback() && !public_address(IpAddr::V6(ip)) {
                return Err(invalid());
            }
            ip.is_loopback()
        }
        Host::Domain(host) => {
            if host.eq_ignore_ascii_case("localhost") {
                true
            } else {
                if !host.contains('.')
                    || host.ends_with('.')
                    || host.ends_with(".localhost")
                    || host.ends_with(".local")
                    || host.ends_with(".internal")
                {
                    return Err(invalid());
                }
                false
            }
        }
    };
    if local && !allow_loopback {
        bail!("Loopback upstreams require allow_loopback_upstreams=true (local testing only)");
    }
    if url.scheme() != "https" && !(allow_loopback && local) {
        return Err(invalid());
    }
    Ok(url)
}

struct PublicResolver {
    allow_loopback: bool,
}

impl Resolve for PublicResolver {
    fn resolve(&self, name: Name) -> Resolving {
        let allow_loopback = self.allow_loopback;
        Box::pin(async move {
            // Explicit localhost never depends on DNS or hosts-file changes.
            if name.as_str().eq_ignore_ascii_case("localhost") && allow_loopback {
                let addresses = vec![
                    SocketAddr::new(IpAddr::V4(Ipv4Addr::LOCALHOST), 0),
                    SocketAddr::new(IpAddr::V6(Ipv6Addr::LOCALHOST), 0),
                ];
                return Ok(Box::new(addresses.into_iter()) as Addrs);
            }
            let addresses: Vec<_> = tokio::net::lookup_host((name.as_str(), 0)).await?.collect();
            // Validate exactly the addresses returned to the connector (no second DNS lookup).
            // Reject mixed public/private DNS answers instead of selecting a convenient one.
            if addresses.is_empty()
                || addresses
                    .iter()
                    .any(|address| !public_address(address.ip()))
            {
                return Err(std::io::Error::new(
                    std::io::ErrorKind::PermissionDenied,
                    "Upstream DNS address rejected",
                )
                .into());
            }
            Ok(Box::new(addresses.into_iter()) as Addrs)
        })
    }
}

pub(super) fn client(settings: &super::Settings) -> Result<reqwest::Client> {
    reqwest::Client::builder()
        .no_proxy()
        .redirect(reqwest::redirect::Policy::none())
        .dns_resolver(Arc::new(PublicResolver {
            allow_loopback: settings.allow_loopback_upstreams,
        }))
        .connect_timeout(Duration::from_secs_f64(settings.connect_timeout_seconds))
        .read_timeout(Duration::from_secs_f64(settings.read_timeout_seconds))
        .pool_idle_timeout(Duration::from_secs(30))
        .pool_max_idle_per_host(8)
        .user_agent("polcia-responses-proxy/2.0")
        .build()
        .map_err(|_| anyhow::anyhow!("Cannot initialize secure HTTP client"))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn rejects_private_and_special_addresses() {
        for value in [
            "0.0.0.0",
            "10.0.0.1",
            "127.0.0.1",
            "169.254.169.254",
            "172.16.1.1",
            "192.168.1.1",
            "100.64.0.1",
            "198.18.0.1",
            "192.0.2.1",
            "224.0.0.1",
            "255.255.255.255",
            "::",
            "::1",
            "fc00::1",
            "fe80::1",
            "::ffff:127.0.0.1",
            "2002:7f00:1::",
            "2001:db8::1",
        ] {
            assert!(!public_address(value.parse().unwrap()), "{value}");
        }
        for value in ["1.1.1.1", "8.8.8.8", "2606:4700:4700::1111"] {
            assert!(public_address(value.parse().unwrap()), "{value}");
        }
    }
    #[test]
    fn url_policy_is_fail_closed_and_sanitized() {
        for value in [
            "http://api.example.com/v1",
            "https://private-value@api.example.com/v1",
            "https://api.example.com/v1?key=private-value",
            "https://api.example.com/v1#private-value",
            "https://169.254.169.254/v1",
            "https://[::ffff:127.0.0.1]/v1",
            "https://api.example.com/v1/responses",
        ] {
            let error = validate_url(value, false).unwrap_err().to_string();
            assert!(!error.contains("private-value"));
        }
        assert!(validate_url("http://127.0.0.1:8080/v1", false).is_err());
        assert!(validate_url("http://127.0.0.1:8080/v1", true).is_ok());
        assert!(validate_url("http://192.168.1.1/v1", true).is_err());
        assert!(validate_url("https://api.example.com/v1", false).is_ok());
    }
}
