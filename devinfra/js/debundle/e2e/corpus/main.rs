use debundle_e2e_corpus::cases;
use libtest_mimic::{Arguments, Trial};

fn main() {
    let args = Arguments::from_args();
    let trials = cases()
        .into_iter()
        .map(|case| {
            Trial::test(case.name().to_owned(), move || {
                case.run();
                Ok(())
            })
        })
        .collect();
    libtest_mimic::run(&args, trials).exit();
}
