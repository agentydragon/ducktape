use asset::Asset;
use chrono::prelude::*;
use denomination::Denomination::*;
use exchange_rate::ExchangeRate;
use json_output::{ConverterSnapshot, ConverterType::*, Snapshot, SourceSnapshot, SourceType::*};
use rust_decimal_macros::*;

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
    let expected = Snapshot {
        timestamp: DateTime::parse_from_rfc3339("2001-01-02T12:34:56+01:00").unwrap(),
        source_snapshot: vec![
            SourceSnapshot {
                id: "a".to_string(),
                name: "A".to_string(),
                source_type: Hardcoded,
                snapshot: vec![Asset {
                    denomination: Currency {
                        currency: "A".to_string(),
                    },
                    amount: dec!(1.23),
                }],
            },
            SourceSnapshot {
                id: "b".to_string(),
                name: "B".to_string(),
                source_type: IBFlex,
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
        converter_snapshots: vec![ConverterSnapshot {
            id: "alphavantage".to_string(),
            converter_type: AlphaVantage,
            snapshot: vec![ExchangeRate {
                from: Currency {
                    currency: "USD".to_string(),
                },
                to: Stock {
                    stock: "GOOG".to_string(),
                },
                rate: dec!(0.0004),
            }],
        }],
        total: Asset {
            denomination: Currency {
                currency: "CHF".to_string(),
            },
            amount: dec!(1234),
        },
    };

    assert_eq!(expected, parsed);
}

#[test]
fn snapshot_round_trips_original_field_names() {
    let saved = serde_json::json!({
        "Timestamp": "2001-01-02T12:34:56+01:00",
        "SourceSnapshot": [{
            "Id": "broker",
            "Name": "Broker",
            "Type": "ibflex",
            "Snapshot": [{"Type": "stock", "Symbol": "GOOG", "Amount": dec!(2.5)}],
        }],
        "ConverterSnapshots": [{
            "Id": "alpha",
            "Type": "alphavantage",
            "Snapshot": [{
                "Source": {"Type": "stock", "Symbol": "GOOG"},
                "Target": {"Type": "currency", "Symbol": "USD"},
                "TargetPerSource": dec!(123.45),
            }]
        }],
        "Total": {"Type": "currency", "Symbol": "USD", "Amount": dec!(308.625)}
    });
    let snapshot: Snapshot = serde_json::from_value(saved.clone()).unwrap();

    assert_eq!(serde_json::to_value(snapshot).unwrap(), saved);
}

#[test]
fn historical_fixer_snapshot_remains_readable() {
    let saved = serde_json::json!({"Id": "fixer", "Type": "fixer", "Snapshot": []});
    let snapshot: json_output::ConverterSnapshot = serde_json::from_value(saved.clone()).unwrap();

    assert_eq!(snapshot.converter_type, json_output::ConverterType::Fixer);
    assert_eq!(serde_json::to_value(snapshot).unwrap(), saved);
}
