use clap::Parser;
use flags::{Command, Opt};

#[test]
fn test_flag_parsing() {
    assert_eq!(
        Opt::parse_from(["worthy", "--command=csv"]),
        Opt {
            command: Command::Csv,
        }
    );
}
