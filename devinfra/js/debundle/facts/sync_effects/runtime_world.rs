//! Application-only mutable-access certificates. Tokens are not semantic facts
//! and the shared allocator is never rewound with a path or root attempt.
use super::world::World;
use std::sync::{
    Arc,
    atomic::{AtomicU64, Ordering},
};

#[derive(Clone)]
pub(super) struct RuntimeWorld {
    world: Arc<World>,
    version: u64,
    next: Arc<AtomicU64>,
}

impl RuntimeWorld {
    pub(super) fn new(world: World) -> Self {
        Self {
            world: Arc::new(world),
            version: 0,
            next: Arc::new(AtomicU64::new(1)),
        }
    }
    fn mint(&self) -> u64 {
        self.next
            .fetch_update(Ordering::Relaxed, Ordering::Relaxed, |next| {
                next.checked_add(1)
            })
            .expect("World mutable-access token exhausted")
    }
    pub(super) fn version(&self) -> u64 {
        self.version
    }
    pub(super) fn make_mut(&mut self) -> &mut World {
        self.version = self.mint();
        Arc::make_mut(&mut self.world)
    }
    pub(super) fn joined(&self, world: World) -> Self {
        Self {
            world: Arc::new(world),
            version: self.mint(),
            next: self.next.clone(),
        }
    }
}

impl std::ops::Deref for RuntimeWorld {
    type Target = World;
    fn deref(&self) -> &World {
        &self.world
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::facts::sync_effects::world::WorldScope;
    #[test]
    fn siblings_and_rewinds_never_reuse_mutable_access_tokens() {
        let base = RuntimeWorld::new(World::new(WorldScope::Application));
        let mut left = base.clone();
        let mut right = base.clone();
        left.make_mut();
        right.make_mut();
        assert_ne!(left.version(), right.version());
        assert!(left.same_contents(&right));
        let old = left.version();
        left = base.clone();
        left.make_mut();
        assert_ne!(left.version(), old);
        assert_ne!(left.joined((*left).clone()).version(), left.version());
    }
}
