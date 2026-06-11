use denomination::Denomination;
use exchange_rate::ExchangeRate;
use rust_decimal_macros::*;

#[test]
fn one_conversion() {
    // 1 USD is ~30 CZK.
    let _ = env_logger::builder().is_test(true).try_init();
    let usd = Denomination::Currency {
        currency: "USD".to_string(),
    };
    let czk = Denomination::Currency {
        currency: "CZK".to_string(),
    };
    let result = common_currency::in_common_currency(
        &[ExchangeRate {
            from: usd.clone(),
            to: czk.clone(),
            rate: dec!(30),
        }],
        &czk,
    );
    println!("{result:?}");
    assert!((result[&usd] - dec!(30)).abs() < dec!(0.001));
}

#[test]
fn two_conversions_chain() {
    // 1 USD is 30 CZK, 1 CZK is 0.2 PLZ
    // So 1 USD should be 6 PLZ.
    let _ = env_logger::builder().is_test(true).try_init();
    let usd = Denomination::Currency {
        currency: "USD".to_string(),
    };
    let czk = Denomination::Currency {
        currency: "CZK".to_string(),
    };
    let plz = Denomination::Currency {
        currency: "PLZ".to_string(),
    };
    let result = common_currency::in_common_currency(
        &[
            ExchangeRate {
                from: usd.clone(),
                to: czk.clone(),
                rate: dec!(30),
            },
            ExchangeRate {
                from: czk,
                to: plz.clone(),
                rate: dec!(0.2),
            },
        ],
        &plz,
    );
    println!("{result:?}");
    assert!((result[&usd] - dec!(6.0)).abs() < dec!(0.001));
}
