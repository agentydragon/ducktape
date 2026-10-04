//! Bounded Hall-deficiency witnesses for candidate-owner domains.
//!
//! Each domain is the set of right-side owner ordinals available to one
//! distinct left-side claim. A maximum matching that cannot cover every left
//! vertex proves a Hall deficiency. Alternating reachability from all
//! unmatched left vertices then returns one such deficient set.

use std::collections::{BTreeMap, VecDeque};

const MAX_LEFT_VERTICES: usize = 256;
const MAX_INPUT_EDGES: usize = 25_600;
const MAX_EDGE_VISITS: usize = 1_000_000;

/// Return a deterministic, nonempty Hall-deficient set of left vertex indices,
/// or `None` when all left vertices can be matched or a work bound is reached.
///
/// `domains` is expected to contain sorted, deduplicated owner ordinals. The
/// ordinals are compressed to a dense, sorted right-side index space, so large
/// sparse ordinals do not cause large allocations. The matching and witness
/// search are iterative and share a one-million-edge-visit budget.
pub fn deficient_set(domains: &[Vec<usize>]) -> Option<Vec<usize>> {
    let left_count = domains.len();
    if left_count == 0 || left_count > MAX_LEFT_VERTICES {
        return None;
    }

    let mut edge_count = 0usize;
    for domain in domains {
        edge_count = edge_count.checked_add(domain.len())?;
        if edge_count > MAX_INPUT_EDGES {
            return None;
        }
    }

    // Singleton claims that share an owner are already a complete
    // two-vertex Hall witness. Find the earliest collision in left-index order
    // before building the general matching graph.
    let mut first_singleton_claim = BTreeMap::new();
    for (left, domain) in domains.iter().enumerate() {
        let [owner] = domain.as_slice() else {
            continue;
        };
        if let Some(&first_left) = first_singleton_claim.get(owner) {
            return Some(vec![first_left, left]);
        }
        first_singleton_claim.insert(*owner, left);
    }

    let mut owners = Vec::with_capacity(edge_count);
    for domain in domains {
        owners.extend_from_slice(domain);
    }
    owners.sort_unstable();
    owners.dedup();

    let adjacency: Vec<Vec<usize>> = domains
        .iter()
        .map(|domain| {
            domain
                .iter()
                .map(|owner| owners.binary_search(owner).expect("owner was collected"))
                .collect()
        })
        .collect();
    let right_count = owners.len();

    // `left_match[l] = r` and `right_match[r] = l` describe the current
    // matching. Processing each left vertex once with a complete augmenting
    // path search leaves a maximum matching of every processed prefix, hence
    // of the full graph.
    let mut left_match: Vec<Option<usize>> = vec![None; left_count];
    let mut right_match: Vec<Option<usize>> = vec![None; right_count];
    let mut edge_visits = 0usize;

    for root in 0..left_count {
        let mut seen_left = vec![false; left_count];
        let mut seen_right = vec![false; right_count];
        let mut predecessor_left = vec![None; right_count];
        let mut queue = VecDeque::new();
        seen_left[root] = true;
        queue.push_back(root);

        let mut free_right = None;
        'search: while let Some(left) = queue.pop_front() {
            for &right in &adjacency[left] {
                if !visit_edge(&mut edge_visits) {
                    return None;
                }
                // Alternating paths leave a left vertex only over unmatched
                // edges. Its matched edge is the edge by which it was reached.
                if left_match[left] == Some(right) || seen_right[right] {
                    continue;
                }
                seen_right[right] = true;
                predecessor_left[right] = Some(left);

                match right_match[right] {
                    Some(next_left) => {
                        if !seen_left[next_left] {
                            seen_left[next_left] = true;
                            queue.push_back(next_left);
                        }
                    }
                    None => {
                        free_right = Some(right);
                        break 'search;
                    }
                }
            }
        }

        // Flip the path back from its free right endpoint. For each left
        // vertex, its prior matched right is the next edge toward the root.
        let Some(mut right) = free_right else {
            continue;
        };
        loop {
            let left = predecessor_left[right]?;
            let previous_right = left_match[left];
            left_match[left] = Some(right);
            right_match[right] = Some(left);
            match previous_right {
                Some(previous_right) => right = previous_right,
                None => break,
            }
        }
    }

    if left_match.iter().all(Option::is_some) {
        return None;
    }

    // The alternating closure of all unmatched left vertices has left side S
    // and right side exactly N(S). Since the matching is maximum, every
    // reached right vertex is matched; its distinct matched left is reached
    // too. At least one unmatched left is also in S, so |N(S)| < |S|.
    let mut reached_left = vec![false; left_count];
    let mut reached_right = vec![false; right_count];
    let mut queue = VecDeque::new();
    for left in 0..left_count {
        if left_match[left].is_none() {
            reached_left[left] = true;
            queue.push_back(left);
        }
    }
    while let Some(left) = queue.pop_front() {
        for &right in &adjacency[left] {
            if !visit_edge(&mut edge_visits) {
                return None;
            }
            if reached_right[right] {
                continue;
            }
            reached_right[right] = true;
            if let Some(matched_left) = right_match[right]
                && !reached_left[matched_left]
            {
                reached_left[matched_left] = true;
                queue.push_back(matched_left);
            }
        }
    }

    let witness: Vec<usize> = reached_left
        .iter()
        .enumerate()
        .filter_map(|(left, reached)| reached.then_some(left))
        .collect();
    // Keep the public guarantee explicit even if this implementation changes.
    let neighbor_count = reached_right.iter().filter(|reached| **reached).count();
    (neighbor_count < witness.len()).then_some(witness)
}

fn visit_edge(edge_visits: &mut usize) -> bool {
    if *edge_visits >= MAX_EDGE_VISITS {
        return false;
    }
    *edge_visits += 1;
    true
}

#[cfg(test)]
mod tests {
    use super::{MAX_INPUT_EDGES, MAX_LEFT_VERTICES, deficient_set};

    fn assert_deficient(domains: &[Vec<usize>], witness: &[usize]) {
        assert!(!witness.is_empty());
        assert!(witness.iter().all(|&left| left < domains.len()));
        let mut lefts = witness.to_vec();
        lefts.sort_unstable();
        lefts.dedup();
        assert_eq!(lefts.len(), witness.len());

        let mut neighbors = Vec::new();
        for &left in witness {
            neighbors.extend_from_slice(&domains[left]);
        }
        neighbors.sort_unstable();
        neighbors.dedup();
        assert!(neighbors.len() < witness.len());
    }

    fn has_full_matching(domains: &[Vec<usize>], right_count: usize) -> bool {
        let mut reachable = vec![false; 1usize << right_count];
        reachable[0] = true;
        for domain in domains {
            let mut next = vec![false; reachable.len()];
            for (mask, was_reachable) in reachable.iter().copied().enumerate() {
                if !was_reachable {
                    continue;
                }
                for &right in domain {
                    let bit = 1usize << right;
                    if mask & bit == 0 {
                        next[mask | bit] = true;
                    }
                }
            }
            reachable = next;
        }
        reachable
            .iter()
            .enumerate()
            .any(|(mask, reachable)| *reachable && mask.count_ones() as usize == domains.len())
    }

    #[test]
    fn duplicate_singleton_claims_have_a_witness() {
        let domains = vec![vec![0], vec![0]];
        let witness = deficient_set(&domains).expect("two claims cannot share one owner");
        assert_eq!(witness, vec![0, 1]);
        assert_deficient(&domains, &witness);
    }

    #[test]
    fn duplicate_singletons_skip_unrelated_domains() {
        let mut domains: Vec<Vec<usize>> =
            (0..128).map(|left| vec![left * 2, left * 2 + 1]).collect();
        domains.push(vec![10_000]);
        domains.push(vec![20_000, 20_001]);
        domains.push(vec![10_000]);

        assert_eq!(deficient_set(&domains), Some(vec![128, 130]));
    }

    #[test]
    fn output_is_deterministic() {
        let domains = vec![vec![3, 8], vec![3, 8], vec![8]];
        let first = deficient_set(&domains);
        for _ in 0..10 {
            assert_eq!(deficient_set(&domains), first);
        }
    }

    #[test]
    fn exhaustive_small_graphs_match_hall_existence() {
        let right_count = 3;
        for left_count in 1..=4 {
            let graph_count = (1usize << right_count).pow(left_count as u32);
            for mut encoded in 0..graph_count {
                let mut domains = Vec::with_capacity(left_count);
                for _ in 0..left_count {
                    let mask = encoded % (1usize << right_count);
                    encoded /= 1usize << right_count;
                    domains.push(
                        (0..right_count)
                            .filter(|right| mask & (1usize << right) != 0)
                            .collect(),
                    );
                }

                let full_matching = has_full_matching(&domains, right_count);
                match deficient_set(&domains) {
                    Some(witness) => {
                        assert!(!full_matching, "graph unexpectedly had a full matching");
                        assert_deficient(&domains, &witness);
                    }
                    None => assert!(
                        full_matching,
                        "graph without a full matching lacked a witness"
                    ),
                }
            }
        }
    }

    #[test]
    fn oversized_inputs_return_no_witness() {
        let too_many_left = vec![Vec::new(); MAX_LEFT_VERTICES + 1];
        assert_eq!(deficient_set(&too_many_left), None);

        let too_many_edges = vec![vec![0; MAX_INPUT_EDGES + 1]];
        assert_eq!(deficient_set(&too_many_edges), None);
    }

    #[test]
    fn edge_visit_budget_exhaustion_returns_no_witness() {
        // The first 100 left vertices match the 100 owners. Every later
        // augmenting search explores the same dense alternating component;
        // this exceeds the fixed visit budget before producing any witness.
        let domains = vec![(0..100).collect::<Vec<_>>(); MAX_LEFT_VERTICES];
        assert!(domains.iter().map(Vec::len).sum::<usize>() <= MAX_INPUT_EDGES);
        assert_eq!(deficient_set(&domains), None);
    }
}
