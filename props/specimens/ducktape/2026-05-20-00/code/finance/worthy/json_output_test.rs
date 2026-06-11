use chrono::prelude::*;
use json_output::{
    Asset, Conversion, ConverterSnapshot, ConverterType::*, Denomination, Denomination::*,
    Snapshot, SourceSnapshot, SourceType, SourceType::*,
};
use rust_decimal_macros::*;

#[test]
fn parse_asset() {
    let json = r#"{"Type": "currency", "Symbol": "A", "Amount": 1.23}"#;
    let parsed: Asset = serde_json::from_str(json).expect("could not parse");
    println!("{parsed:#?}");

    let expected = Asset {
        denomination: Denomination::Currency {
            symbol: "A".to_string(),
        },
        amount: dec!(1.23),
    };

    assert_eq!(expected, parsed);
}

#[test]
fn parse_snapshot() {
    let json = r#"
        {
          "Timestamp": "2001-01-02T12:34:56+01:00",
          "SourceSnapshot": [
            {
              "Id": "a",
              "Name": "A",
              "Type": "hardcoded",
              "Snapshot": [{"Type": "currency", "Symbol": "A", "Amount": 1.23}]
            },
            {
              "Id": "b",
              "Name": "B",
              "Type": "ibflex",
              "Snapshot": [
                {"Type": "crypto", "Symbol": "AAA", "Amount": 1},
                {"Type": "crypto", "Symbol": "BBB", "Amount": 2},
                {"Type": "crypto", "Symbol": "CCC", "Amount": 3}
              ]
            }
          ],
          "ConverterSnapshots": [
            {
              "Id": "currencylayer",
              "Type": "currencylayer",
              "Params": {},
              "Snapshot": [
                {
                  "Source": {"Type": "currency", "Symbol": "CHF"},
                  "Target": {"Type": "currency", "Symbol": "USD"},
                  "TargetPerSource": 1.1
                },
                {
                  "Source": {"Type": "currency", "Symbol": "EUR"},
                  "Target": {"Type": "currency", "Symbol": "USD"},
                  "TargetPerSource": 2.2
                }
              ]
            },
            {
              "Id": "alphavantage",
              "Type": "alphavantage",
              "Params": {},
              "Snapshot": [
                {
                  "Source": {"Type": "currency", "Symbol": "USD"},
                  "Target": {"Type": "stock", "Symbol": "GOOG"},
                  "TargetPerSource": 0.0004
                }
              ]
            }
          ],
          "Total": {
            "Type": "currency",
            "Symbol": "CHF",
            "Amount": 1234
          }
        }
    "#;
    let parsed: Snapshot = serde_json::from_str(json).expect("could not parse");
    println!("{parsed:#?}");

    //denomination: Denomination::Currency {
    //    symbol: "CHF".to_string(),
    //},
    //amount: dec!(1234),
    let expected = Snapshot {
        timestamp: DateTime::parse_from_rfc3339("2001-01-02T12:34:56+01:00").unwrap(),
        source_snapshot: vec![
            SourceSnapshot {
                id: "a".to_string(),
                name: "A".to_string(),
                source_type: Hardcoded,
                snapshot: vec![Asset {
                    denomination: Currency {
                        symbol: "A".to_string(),
                    },
                    amount: dec!(1.23),
                }],
            },
            SourceSnapshot {
                id: "b".to_string(),
                name: "B".to_string(),
                source_type: SourceType::IBFlex,
                snapshot: vec![
                    Asset {
                        denomination: Cryptocurrency {
                            symbol: "AAA".to_string(),
                        },
                        amount: dec!(1),
                    },
                    Asset {
                        denomination: Cryptocurrency {
                            symbol: "BBB".to_string(),
                        },
                        amount: dec!(2),
                    },
                    Asset {
                        denomination: Cryptocurrency {
                            symbol: "CCC".to_string(),
                        },
                        amount: dec!(3),
                    },
                ],
            },
        ],
        converter_snapshots: vec![
            ConverterSnapshot {
                id: "currencylayer".to_string(),
                converter_type: CurrencyLayer,
                snapshot: vec![
                    Conversion {
                        source: Currency {
                            symbol: "CHF".to_string(),
                        },
                        target: Currency {
                            symbol: "USD".to_string(),
                        },
                        target_per_source: dec!(1.1),
                    },
                    Conversion {
                        source: Currency {
                            symbol: "EUR".to_string(),
                        },
                        target: Currency {
                            symbol: "USD".to_string(),
                        },
                        target_per_source: dec!(2.2),
                    },
                ],
            },
            ConverterSnapshot {
                id: "alphavantage".to_string(),
                converter_type: AlphaVantage,
                snapshot: vec![Conversion {
                    source: Currency {
                        symbol: "USD".to_string(),
                    },
                    target: Stock {
                        symbol: "GOOG".to_string(),
                    },
                    target_per_source: dec!(0.0004),
                }],
            },
        ],
        total: Asset {
            denomination: Currency {
                symbol: "CHF".to_string(),
            },
            amount: dec!(1234),
        },
    };

    assert_eq!(expected, parsed);
}
