//! A growable bit set over small integer ids.

#[derive(Default, Clone)]
pub struct Bits {
    words: Vec<u64>,
    len: u32,
}

impl Bits {
    /// Inserts `id`; returns whether it was absent.
    pub fn insert(&mut self, id: u32) -> bool {
        let word = (id / 64) as usize;
        if word >= self.words.len() {
            self.words.resize(word + 1, 0);
        }
        let bit = 1u64 << (id % 64);
        let absent = self.words[word] & bit == 0;
        if absent {
            self.words[word] |= bit;
            self.len += 1;
        }
        absent
    }

    pub fn contains(&self, id: u32) -> bool {
        self.words
            .get((id / 64) as usize)
            .is_some_and(|word| word & (1u64 << (id % 64)) != 0)
    }

    pub fn len(&self) -> usize {
        self.len as usize
    }

    pub fn is_empty(&self) -> bool {
        self.len == 0
    }

    pub fn iter(&self) -> impl Iterator<Item = u32> + '_ {
        self.words.iter().enumerate().flat_map(|(index, word)| {
            let mut rest = *word;
            std::iter::from_fn(move || {
                if rest == 0 {
                    return None;
                }
                let bit = rest.trailing_zeros();
                rest &= rest - 1;
                Some(index as u32 * 64 + bit)
            })
        })
    }
}
