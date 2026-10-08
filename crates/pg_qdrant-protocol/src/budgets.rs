//! Arithmetic-only admission contract for bounded future native requests.
//! This is NOT an RSS/mmap or native cancellation enforcement mechanism.

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct RequestBudget {
    pub top_k: usize,
    pub candidates: usize,
    pub query_tokens: usize,
    pub vector_dimensions: usize,
    pub rerank_rows: usize,
    pub rerank_tokens_per_row: usize,
    pub requested_response_bytes: usize,
    pub timeout_ms: u64,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum BudgetError {
    Zero,
    TopK,
    Candidates,
    Tokens,
    Dimensions,
    RerankCells,
    ResponseBytes,
    Timeout,
}

pub const TOP_K_MAX: usize = 100;
pub const CANDIDATES_MAX: usize = 1_000;
pub const QUERY_TOKENS_MAX: usize = 4_096;
pub const DIMENSIONS_MAX: usize = 4_096;
pub const MATRIX_CELLS_MAX: usize = 1_000_000;
pub const RESPONSE_BYTES_MAX: usize = 1024 * 1024;
pub const TIMEOUT_MS_MAX: u64 = 30_000;

impl RequestBudget {
    pub fn admit(&self) -> Result<(), BudgetError> {
        if self.top_k == 0 || self.candidates == 0 {
            return Err(BudgetError::Zero);
        }
        if self.top_k > TOP_K_MAX {
            return Err(BudgetError::TopK);
        }
        if self.candidates > CANDIDATES_MAX || self.candidates < self.top_k {
            return Err(BudgetError::Candidates);
        }
        if self.query_tokens > QUERY_TOKENS_MAX {
            return Err(BudgetError::Tokens);
        }
        if self.vector_dimensions > DIMENSIONS_MAX {
            return Err(BudgetError::Dimensions);
        }
        let total = self.rerank_rows
            .checked_mul(self.rerank_tokens_per_row)
            .and_then(|cells| cells.checked_mul(self.vector_dimensions))
            .ok_or(BudgetError::RerankCells)?;
        if total > MATRIX_CELLS_MAX {
            return Err(BudgetError::RerankCells);
        }
        if self.requested_response_bytes > RESPONSE_BYTES_MAX {
            return Err(BudgetError::ResponseBytes);
        }
        if self.timeout_ms == 0 || self.timeout_ms > TIMEOUT_MS_MAX {
            return Err(BudgetError::Timeout);
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn valid() -> RequestBudget {
        RequestBudget {
            top_k: 10, candidates: 50, query_tokens: 32,
            vector_dimensions: 256, rerank_rows: 50,
            rerank_tokens_per_row: 4, requested_response_bytes: 8192,
            timeout_ms: 5000,
        }
    }
    #[test]
    fn bounded_request_is_accepted() {
        assert_eq!(valid().admit(), Ok(()));
    }
    #[test]
    fn overflow_refuses_even_when_other_fields_look_valid() {
        let mut req = valid();
        req.rerank_rows = usize::MAX;
        assert_eq!(req.admit(), Err(BudgetError::RerankCells));
    }
    #[test]
    fn excessive_candidate_or_result_is_rejected() {
        let mut req = valid();
        req.candidates = 1001;
        assert_eq!(req.admit(), Err(BudgetError::Candidates));
        req = valid();
        req.top_k = 101;
        assert_eq!(req.admit(), Err(BudgetError::TopK));
    }
    #[test]
    fn matrix_and_response_bounds_are_independent() {
        let mut req = valid();
        req.rerank_rows = 1000;
        req.rerank_tokens_per_row = 1000;
        assert_eq!(req.admit(), Err(BudgetError::RerankCells));
        req = valid();
        req.requested_response_bytes = RESPONSE_BYTES_MAX + 1;
        assert_eq!(req.admit(), Err(BudgetError::ResponseBytes));
    }
    #[test]
    fn deadline_is_never_unbounded() {
        let mut req = valid();
        req.timeout_ms = 0;
        assert_eq!(req.admit(), Err(BudgetError::Timeout));
        req.timeout_ms = TIMEOUT_MS_MAX + 1;
        assert_eq!(req.admit(), Err(BudgetError::Timeout));
    }
}