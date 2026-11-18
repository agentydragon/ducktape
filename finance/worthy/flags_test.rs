use flags::{Command, Opt};
use structopt::StructOpt;

#[test]
fn test_flag_parsing() {
    assert_eq!(
        Opt::from_iter(&["worthy", "--command=csv"]),
        Opt {
            command: Command::Csv,
        }
    );
}
