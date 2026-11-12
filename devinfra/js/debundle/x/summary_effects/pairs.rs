//! The pair check: which pairs of top-level source units have conflicting
//! effect sets, by a sweep over each cell's readers and writers.
//!
//! Two units conflict when one writes a cell the other reads or writes. A
//! unit that may call into Escaped additionally carries the Escaped summary;
//! membership is one flag per unit, so that summary is never copied.

use rustc_hash::FxHashMap;

use crate::effects::{Cell, CellId, CellTable, Effects};
use crate::model::{Model, Obj};

pub const VAR_TOP: u8 = 1;
pub const VAR_LOCAL: u8 = 2;
pub const FIELD: u8 = 4;
pub const ANY_PROP: u8 = 8;
pub const FN_ALLOC: u8 = 16;
pub const INTRINSIC: u8 = 32;
pub const ESCAPED: u8 = 64;
pub const HAVOC: u8 = 128;

pub const FLAG_NAMES: [(u8, &str); 8] = [
    (VAR_TOP, "top_level_binding"),
    (VAR_LOCAL, "captured_local"),
    (FIELD, "named_property_or_elements"),
    (ANY_PROP, "any_property"),
    (FN_ALLOC, "function_allocated_object"),
    (INTRINSIC, "intrinsic_property"),
    (ESCAPED, "escaped"),
    (HAVOC, "havoc"),
];

pub fn flag_names(flags: u8) -> String {
    FLAG_NAMES
        .iter()
        .filter(|(bit, _)| flags & bit != 0)
        .map(|(_, name)| *name)
        .collect::<Vec<_>>()
        .join("+")
}

#[derive(Clone, Copy)]
struct Participant {
    statement: u32,
    /// The access comes from a computed-key read or write.
    any_prop: bool,
}

pub struct PairCheck {
    pub n: usize,
    /// Conflicts that do not go through a call into Escaped, indexed
    /// `low * n + high`.
    direct: Vec<u8>,
    /// One cell both units touch, for a direct conflict.
    witness: Vec<CellId>,
    /// Per unit: the classes of its cells that the Escaped summary conflicts
    /// with.
    pub exposed: Vec<u8>,
    pub reasons: Vec<u16>,
    pub havoc: Vec<bool>,
    pub cells: Vec<Cell>,
}

const NO_WITNESS: CellId = CellId::MAX;

/// Rewrites computed-key accesses into the cells they cover.
fn expand(
    accesses: &[u32],
    cells: &CellTable,
    fields: &FxHashMap<Obj, Vec<CellId>>,
    stars: &FxHashMap<Obj, [CellId; 2]>,
    intrinsics: &[CellId],
    intrinsic_star: Option<CellId>,
) -> Vec<(CellId, bool, bool)> {
    let mut out = Vec::with_capacity(accesses.len());
    for access in accesses {
        let (cell, write) = (access >> 1, access & 1 == 1);
        match cells.cells[cell as usize] {
            Cell::AnyOf(obj) => {
                out.extend(stars[&obj].map(|cell| (cell, write, true)));
                for field in fields.get(&obj).into_iter().flatten() {
                    out.push((*field, write, true));
                }
            }
            Cell::AnyIntrinsic => {
                out.extend(intrinsic_star.map(|star| (star, write, true)));
                out.extend(intrinsics.iter().map(|cell| (*cell, write, true)));
            }
            _ => out.push((cell, write, false)),
        }
    }
    // A precise access sorts before, and so shadows, an expanded one.
    out.sort_unstable();
    out.dedup_by_key(|(cell, write, _)| (*cell, *write));
    out
}

impl PairCheck {
    pub fn new(m: &Model, effects: &mut Effects, statement_count: usize) -> Self {
        let n = statement_count;
        let any_objects: Vec<Obj> = effects
            .cells
            .cells
            .iter()
            .filter_map(|cell| match cell {
                Cell::AnyOf(obj) => Some(*obj),
                _ => None,
            })
            .collect();
        let mut stars = FxHashMap::default();
        for obj in any_objects {
            let cells = [
                effects.cells.id(Cell::Star(obj)),
                effects.cells.id(Cell::Elems(obj)),
            ];
            stars.insert(obj, cells);
        }
        let has_any_intrinsic = effects.cells.cells.contains(&Cell::AnyIntrinsic);
        let intrinsic_star = has_any_intrinsic.then(|| effects.cells.id(Cell::IntrinsicStar));
        let mut fields: FxHashMap<Obj, Vec<CellId>> = FxHashMap::default();
        let mut intrinsics = Vec::new();
        for (id, cell) in effects.cells.cells.iter().enumerate() {
            match cell {
                Cell::Field(obj, _) => fields.entry(*obj).or_default().push(id as CellId),
                Cell::Intrinsic(_) => intrinsics.push(id as CellId),
                _ => {}
            }
        }
        let cells = &effects.cells;
        let class = |cell: CellId, any_prop: bool| -> u8 {
            let function_allocated = |obj: Obj| -> u8 {
                if m.objs[obj as usize].unit as usize >= n {
                    FN_ALLOC
                } else {
                    0
                }
            };
            match cells.cells[cell as usize] {
                Cell::Var(var) if m.vars[var as usize].top_level => VAR_TOP,
                Cell::Var(_) => VAR_LOCAL,
                Cell::Field(obj, _) if any_prop => ANY_PROP | function_allocated(obj),
                Cell::Field(obj, _) => FIELD | function_allocated(obj),
                Cell::Star(obj) => ANY_PROP | function_allocated(obj),
                Cell::Elems(obj) if any_prop => ANY_PROP | function_allocated(obj),
                Cell::Elems(obj) => FIELD | function_allocated(obj),
                Cell::UnknownHeap => ESCAPED,
                Cell::Intrinsic(_) | Cell::IntrinsicStar => INTRINSIC,
                Cell::AnyOf(_) | Cell::AnyIntrinsic => unreachable!("expanded away"),
            }
        };

        let escaped = expand(
            &effects.escaped.accesses,
            cells,
            &fields,
            &stars,
            &intrinsics,
            intrinsic_star,
        );
        let cell_count = cells.cells.len();
        let mut escaped_reads = vec![false; cell_count];
        let mut escaped_writes = vec![false; cell_count];
        for (cell, write, _) in &escaped {
            if *write {
                escaped_writes[*cell as usize] = true;
            } else {
                escaped_reads[*cell as usize] = true;
            }
        }

        let mut readers: Vec<Vec<Participant>> = vec![Vec::new(); cell_count];
        let mut writers: Vec<Vec<Participant>> = vec![Vec::new(); cell_count];
        let mut exposed = vec![0u8; n];
        for (statement, summary) in effects.statements.iter().enumerate() {
            let accesses = expand(
                &summary.accesses,
                cells,
                &fields,
                &stars,
                &intrinsics,
                intrinsic_star,
            );
            for (cell, write, any_prop) in accesses {
                let participant = Participant {
                    statement: statement as u32,
                    any_prop,
                };
                let conflicts_with_escaped =
                    escaped_writes[cell as usize] || (write && escaped_reads[cell as usize]);
                if conflicts_with_escaped {
                    exposed[statement] |= class(cell, any_prop);
                }
                if write {
                    writers[cell as usize].push(participant);
                } else {
                    readers[cell as usize].push(participant);
                }
            }
        }

        let mut direct = vec![0u8; n * n];
        let mut witness = vec![NO_WITNESS; n * n];
        for cell in 0..cell_count {
            for writer in &writers[cell] {
                for other in writers[cell].iter().chain(&readers[cell]) {
                    if other.statement == writer.statement {
                        continue;
                    }
                    let (low, high) = if writer.statement < other.statement {
                        (writer.statement, other.statement)
                    } else {
                        (other.statement, writer.statement)
                    };
                    let index = low as usize * n + high as usize;
                    direct[index] |= class(cell as CellId, writer.any_prop || other.any_prop);
                    if witness[index] == NO_WITNESS {
                        witness[index] = cell as CellId;
                    }
                }
            }
        }
        Self {
            n,
            direct,
            witness,
            exposed,
            reasons: effects
                .statements
                .iter()
                .map(|summary| summary.reasons)
                .collect(),
            havoc: effects
                .statements
                .iter()
                .map(|summary| summary.havoc)
                .collect(),
            cells: effects.cells.cells.clone(),
        }
    }

    /// Whether the unit may call into Escaped once the reasons in
    /// `resolved` are assumed away.
    pub fn opaque(&self, statement: usize, resolved: u16) -> bool {
        self.reasons[statement] & !resolved != 0
    }

    /// The conflict classes of a pair; zero when the pair commutes.
    pub fn flags(&self, a: usize, b: usize, resolved: u16) -> u8 {
        let (low, high) = if a < b { (a, b) } else { (b, a) };
        let mut flags = self.direct[low * self.n + high];
        let (opaque_low, opaque_high) = (self.opaque(low, resolved), self.opaque(high, resolved));
        if (opaque_low && (opaque_high || self.exposed[high] != 0))
            || (opaque_high && self.exposed[low] != 0)
        {
            flags |= ESCAPED;
        }
        if self.havoc[low] || self.havoc[high] {
            flags |= HAVOC;
        }
        flags
    }

    pub fn witness(&self, a: usize, b: usize) -> Option<Cell> {
        let (low, high) = if a < b { (a, b) } else { (b, a) };
        let cell = self.witness[low * self.n + high];
        (cell != NO_WITNESS).then(|| self.cells[cell as usize])
    }
}

/// Positions of the source units in an emitted order.
pub fn positions(order: &[usize], n: usize) -> Vec<usize> {
    let mut positions = vec![usize::MAX; n];
    for (position, unit) in order.iter().enumerate() {
        positions[*unit] = position;
    }
    assert!(
        positions.iter().all(|position| *position != usize::MAX),
        "the emitted order must name every source unit exactly once",
    );
    positions
}
