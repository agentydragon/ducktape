use std::collections::VecDeque;
use std::sync::{Arc, Mutex as StdMutex, MutexGuard as StdMutexGuard};
use std::time::{Duration, Instant};

use tokio::sync::Notify;
use tokio::time::timeout;

use crate::DEFAULT_MAX_QUEUE_WAIT;

const HEALTH_WINDOW: Duration = Duration::from_secs(30);
const HEALTH_MIN_SAMPLES: usize = 10;

#[derive(Debug, Clone)]
pub struct LimiterConfig {
    pub initial: usize,
    pub min: usize,
    pub max: usize,
    pub queue_wait: Duration,
    pub failure_cooldown: Duration,
    pub severe_failure_cooldown: Duration,
}

impl LimiterConfig {
    pub fn availability() -> Self {
        Self {
            initial: 8,
            min: 1,
            max: 32,
            queue_wait: DEFAULT_MAX_QUEUE_WAIT,
            failure_cooldown: Duration::from_secs(30),
            severe_failure_cooldown: Duration::from_secs(60),
        }
    }

    pub fn cdx() -> Self {
        Self {
            initial: 2,
            min: 1,
            max: 6,
            queue_wait: DEFAULT_MAX_QUEUE_WAIT,
            failure_cooldown: Duration::from_secs(30),
            severe_failure_cooldown: Duration::from_secs(60),
        }
    }

    pub fn replay() -> Self {
        Self {
            initial: 2,
            min: 1,
            max: 8,
            queue_wait: DEFAULT_MAX_QUEUE_WAIT,
            failure_cooldown: Duration::from_secs(30),
            severe_failure_cooldown: Duration::from_secs(60),
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AcquisitionOutcome {
    Healthy,
    TransientFailure,
    RetryAfter(Duration),
}

#[derive(Debug)]
struct LimiterEvent {
    at: Instant,
    failed: bool,
}

#[derive(Debug)]
struct LimiterState {
    current_limit: usize,
    in_flight: usize,
    waiters: usize,
    backoff_until: Option<Instant>,
    events: VecDeque<LimiterEvent>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LimiterSnapshot {
    pub current_limit: usize,
    pub in_flight: usize,
    pub waiters: usize,
    pub backoff_seconds: Option<u64>,
    pub recent_events: usize,
    pub recent_failures: usize,
}

#[derive(Debug)]
pub struct AdaptiveLimiter {
    config: LimiterConfig,
    state: StdMutex<LimiterState>,
    notify: Notify,
}

#[derive(Debug)]
#[must_use = "dropping a limiter permit releases its in-flight slot"]
pub struct LimiterPermit {
    limiter: Option<Arc<AdaptiveLimiter>>,
}

#[derive(Debug)]
struct LimiterWaitGuard {
    limiter: Option<Arc<AdaptiveLimiter>>,
}

impl LimiterPermit {
    fn new(limiter: Arc<AdaptiveLimiter>) -> Self {
        Self {
            limiter: Some(limiter),
        }
    }

    pub fn record(mut self, outcome: AcquisitionOutcome) {
        if let Some(limiter) = self.limiter.take() {
            limiter.release(Some(outcome));
        }
    }
}

impl Drop for LimiterPermit {
    fn drop(&mut self) {
        if let Some(limiter) = self.limiter.take() {
            limiter.release(None);
        }
    }
}

impl LimiterWaitGuard {
    fn new(limiter: Arc<AdaptiveLimiter>) -> Self {
        {
            let mut state = limiter.lock_state();
            state.waiters += 1;
        }
        Self {
            limiter: Some(limiter),
        }
    }
}

impl Drop for LimiterWaitGuard {
    fn drop(&mut self) {
        if let Some(limiter) = self.limiter.take() {
            let mut state = limiter.lock_state();
            state.waiters = state.waiters.saturating_sub(1);
        }
    }
}

impl AdaptiveLimiter {
    pub fn new(config: LimiterConfig) -> Self {
        Self {
            state: StdMutex::new(LimiterState {
                current_limit: config.initial,
                in_flight: 0,
                waiters: 0,
                backoff_until: None,
                events: VecDeque::new(),
            }),
            config,
            notify: Notify::new(),
        }
    }

    pub async fn acquire(self: Arc<Self>) -> Option<LimiterPermit> {
        let deadline = Instant::now() + self.config.queue_wait;
        loop {
            let wait = {
                let mut state = self.lock_state();
                let now = Instant::now();
                if state.backoff_until.is_some_and(|until| until <= now) {
                    state.backoff_until = None;
                }
                if state.backoff_until.is_none() && state.in_flight < state.current_limit {
                    state.in_flight += 1;
                    return Some(LimiterPermit::new(self.clone()));
                }
                deadline.checked_duration_since(now)
            };
            let wait = wait?;
            let waiter = LimiterWaitGuard::new(self.clone());
            if timeout(wait, self.notify.notified()).await.is_err() {
                return None;
            }
            drop(waiter);
        }
    }

    pub async fn retry_after(&self) -> Option<Duration> {
        let mut state = self.lock_state();
        let now = Instant::now();
        if state.backoff_until.is_some_and(|until| until <= now) {
            state.backoff_until = None;
        }
        state
            .backoff_until
            .and_then(|until| until.checked_duration_since(now))
    }

    fn release(&self, outcome: Option<AcquisitionOutcome>) {
        let mut state = self.lock_state();
        state.in_flight = state.in_flight.saturating_sub(1);
        if let Some(outcome) = outcome {
            self.record_outcome(&mut state, outcome);
        }
        self.notify.notify_waiters();
    }

    fn record_outcome(&self, state: &mut LimiterState, outcome: AcquisitionOutcome) {
        let now = Instant::now();
        match outcome {
            AcquisitionOutcome::Healthy => self.record_health(state, now, false),
            AcquisitionOutcome::TransientFailure => {
                self.record_health(state, now, true);
                state.current_limit = (state.current_limit / 2).max(self.config.min);
                state.backoff_until = Some(now + self.config.failure_cooldown);
            }
            AcquisitionOutcome::RetryAfter(duration) => {
                self.record_health(state, now, true);
                state.current_limit = self.config.min;
                state.backoff_until = Some(now + duration);
            }
        }
    }

    fn record_health(&self, state: &mut LimiterState, now: Instant, failed: bool) {
        state.events.push_back(LimiterEvent { at: now, failed });
        while state
            .events
            .front()
            .is_some_and(|event| now.duration_since(event.at) > HEALTH_WINDOW)
        {
            state.events.pop_front();
        }
        if state.events.len() < HEALTH_MIN_SAMPLES {
            return;
        }
        let failures = state.events.iter().filter(|event| event.failed).count();
        let failure_rate = failures as f64 / state.events.len() as f64;
        if failure_rate < 0.05 && state.current_limit < self.config.max {
            state.current_limit += 1;
            state.events.clear();
        } else if failure_rate >= 0.5 {
            state.current_limit = self.config.min;
            state.backoff_until = Some(now + self.config.severe_failure_cooldown);
            state.events.clear();
        } else if failure_rate >= 0.2 {
            state.current_limit = (state.current_limit / 2).max(self.config.min);
            state.backoff_until = Some(now + self.config.failure_cooldown);
            state.events.clear();
        }
    }

    pub async fn current_limit(&self) -> usize {
        self.lock_state().current_limit
    }

    pub async fn snapshot(&self) -> LimiterSnapshot {
        let mut state = self.lock_state();
        let now = Instant::now();
        if state.backoff_until.is_some_and(|until| until <= now) {
            state.backoff_until = None;
        }
        while state
            .events
            .front()
            .is_some_and(|event| now.duration_since(event.at) > HEALTH_WINDOW)
        {
            state.events.pop_front();
        }
        LimiterSnapshot {
            current_limit: state.current_limit,
            in_flight: state.in_flight,
            waiters: state.waiters,
            backoff_seconds: state
                .backoff_until
                .and_then(|until| until.checked_duration_since(now))
                .and_then(|duration| retry_after_seconds(Some(duration))),
            recent_events: state.events.len(),
            recent_failures: state.events.iter().filter(|event| event.failed).count(),
        }
    }

    fn lock_state(&self) -> StdMutexGuard<'_, LimiterState> {
        self.state
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }
}

fn retry_after_seconds(duration: Option<Duration>) -> Option<u64> {
    duration.map(|duration| duration.as_secs() + u64::from(duration.subsec_nanos() > 0))
}
