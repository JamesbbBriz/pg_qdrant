#[cfg(not(panic = "unwind"))]
compile_error!("PostgreSQL extension builds require panic unwinding");

use std::sync::atomic::{AtomicBool, Ordering};
static DROPPED: AtomicBool = AtomicBool::new(false);
struct Guard;
impl Drop for Guard {
    fn drop(&mut self) {
        DROPPED.store(true, Ordering::SeqCst);
    }
}
extern "C-unwind" fn callback() {
    let _guard = Guard;
    panic!("compiler unwind qualification");
}
fn main() {
    assert!(std::panic::catch_unwind(|| callback()).is_err());
    assert!(DROPPED.load(Ordering::SeqCst));
    // A minimal native SIMD check precedes the actual Edge SIMD feature tests.
    unsafe {
        use std::arch::x86_64::*;
        let value = _mm_add_epi32(_mm_set1_epi32(20), _mm_set1_epi32(22));
        assert_eq!(_mm_cvtsi128_si32(value), 42);
    }
}
