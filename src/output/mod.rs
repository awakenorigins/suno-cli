pub mod json;
pub mod table;

pub enum OutputFormat {
    Json,
    Table,
}

impl OutputFormat {
    pub fn detect(json_flag: bool) -> Self {
        if json_flag || !std::io::IsTerminal::is_terminal(&std::io::stdout()) {
            OutputFormat::Json
        } else {
            OutputFormat::Table
        }
    }
}
