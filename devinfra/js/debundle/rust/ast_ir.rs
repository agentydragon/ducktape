use serde::Serialize;

#[derive(Debug, Clone, Serialize)]
pub struct ModuleIr {
    pub id: String,
    pub import_specifiers: Vec<String>,
    pub export_count: usize,
    pub has_top_level_effects: bool,
}

#[derive(Debug, Clone, Serialize)]
pub struct ProgramIr {
    pub modules: Vec<ModuleIr>,
}
