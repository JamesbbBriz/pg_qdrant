//! Conservative runtime admission for the fixed Edge 0.8.0 native build.
//!
//! Upstream compiles both x86 quantization C objects with `-march=haswell`.
//! This guard is not an x86-64-v3 declaration or a CPU portability benchmark.
//! Rust feature detection includes OS state for AVX-family execution; the two
//! extra CPUID bits below describe hardware features only. There is no bypass.

pub const BASELINE: &str = "edge-0.8.0-native-haswell-conservative-v1";
pub const REQUIRED: &[&str] = &[
    "avx",
    "avx2",
    "fma",
    "f16c",
    "bmi1",
    "bmi2",
    "lzcnt",
    "movbe",
    "popcnt",
    "sse3",
    "ssse3",
    "sse4.1",
    "sse4.2",
    "cmpxchg16b",
    "pclmulqdq",
    "rdrand",
    "xsave",
    "xsaveopt",
    "lahf_sahf",
    "fsgsbase",
];

#[derive(Debug)]
pub struct Report {
    pub architecture: &'static str,
    pub detected: Vec<(&'static str, bool)>,
    pub missing: Vec<&'static str>,
    pub hle_observed: bool,
}

impl Report {
    pub fn admitted(&self) -> bool {
        self.missing.is_empty()
    }

    pub fn require(&self) -> Result<(), String> {
        if self.admitted() {
            Ok(())
        } else {
            Err(format!(
                "CPU admission refused for {BASELINE}: architecture={}, missing usable features={}. The fixed Edge native build requires this conservative baseline; this Edge entry is refused.",
                self.architecture,
                self.missing.join(", ")
            ))
        }
    }
}

fn evaluate(
    architecture: &'static str,
    detected: Vec<(&'static str, bool)>,
    hle_observed: bool,
) -> Report {
    let mut missing = Vec::new();
    if architecture != "x86_64" {
        missing.push("architecture:x86_64");
    }
    for feature in REQUIRED {
        if !detected
            .iter()
            .any(|(name, usable)| name == feature && *usable)
        {
            missing.push(*feature);
        }
    }
    Report {
        architecture,
        detected,
        missing,
        hle_observed,
    }
}

/// Inspect this process without executing Edge, an instruction requiring the guarded features,
/// or accepting a user-provided feature override. CPUID is available on x86_64.
pub fn report() -> Report {
    #[cfg(target_arch = "x86_64")]
    {
        macro_rules! detected {
            ($($name:tt),+ $(,)?) => {
                vec![$(($name, std::is_x86_feature_detected!($name))),+]
            };
        }
        let mut features = detected!(
            "avx",
            "avx2",
            "fma",
            "f16c",
            "bmi1",
            "bmi2",
            "lzcnt",
            "movbe",
            "popcnt",
            "sse3",
            "ssse3",
            "sse4.1",
            "sse4.2",
            "cmpxchg16b",
            "pclmulqdq",
            "rdrand",
            "xsave",
            "xsaveopt",
        );
        // Check each maximum leaf before reading feature bits. These are real
        // host observations; policy tests below never substitute CPUID output.
        let (lahf_sahf, fsgsbase, hle) = {
            use std::arch::x86_64::{__cpuid, __cpuid_count};
            let extended = __cpuid(0x8000_0000).eax;
            let lahf_sahf = extended >= 0x8000_0001 && (__cpuid(0x8000_0001).ecx & 1) != 0;
            let (fsgsbase, hle) = if __cpuid(0).eax >= 7 {
                let leaf = __cpuid_count(7, 0);
                ((leaf.ebx & 1) != 0, (leaf.ebx & (1 << 4)) != 0)
            } else {
                (false, false)
            };
            (lahf_sahf, fsgsbase, hle)
        };
        features.push(("lahf_sahf", lahf_sahf));
        features.push(("fsgsbase", fsgsbase));
        evaluate(std::env::consts::ARCH, features, hle)
    }
    #[cfg(not(target_arch = "x86_64"))]
    {
        evaluate(std::env::consts::ARCH, Vec::new(), false)
    }
}

pub fn require() -> Result<(), String> {
    report().require()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn complete_policy_observations() -> Vec<(&'static str, bool)> {
        REQUIRED.iter().map(|name| (*name, true)).collect()
    }

    #[test]
    fn missing_or_os_unusable_features_refuse_admission() {
        // Pure admission policy only: false AVX models an unusable OS state,
        // not a simulated CPU/CPUID observation or an unsupported-CPU run.
        for missing in ["avx", "avx2", "lahf_sahf", "fsgsbase"] {
            let mut features = complete_policy_observations();
            features
                .iter_mut()
                .find(|(name, _)| *name == missing)
                .unwrap()
                .1 = false;
            let result = evaluate("x86_64", features, true);
            assert_eq!(result.missing, [missing]);
            assert!(result.require().unwrap_err().contains(missing));
        }
        let mut features = complete_policy_observations();
        features.retain(|(name, _)| *name != "xsaveopt");
        assert_eq!(evaluate("x86_64", features, false).missing, ["xsaveopt"]);
        assert!(!evaluate("aarch64", complete_policy_observations(), false).admitted());
    }

    #[test]
    fn hle_is_observed_but_not_required() {
        assert!(!REQUIRED.contains(&"hle"));
        for hle in [false, true] {
            let result = evaluate("x86_64", complete_policy_observations(), hle);
            assert_eq!(result.hle_observed, hle);
            assert!(result.admitted());
            assert!(result.require().is_ok());
        }
    }
}
