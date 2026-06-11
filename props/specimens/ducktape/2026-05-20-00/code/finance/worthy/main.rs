// RUST_LOG=rust_main=trace bazel run :rust_main

use alphavantage_converter::AlphaVantageConverter;
use asset::Asset;
use chrono::prelude::*;
use config::{Config, ConverterConfig, SourceConfig};
use converter::Converter;
use currencylayer_converter::CurrencyLayerConverter;
use denomination::Denomination;
use exchange_rate::ExchangeRate;
use fixer_converter::FixerConverter;
use flags::Opt;
use futures::prelude::*;
use glob::glob;
use ibflex_source::IBFlexSource;
use log::{info, trace, warn};
use rust_decimal::prelude::*;
use rust_decimal_macros::*;
use rusty_money::{Money, iso};
use source::Source;
use std::collections::{HashMap, HashSet};
use std::error::Error;
use std::ffi::OsStr;
use std::fs::File;
use std::io::prelude::*;
use std::path::Path;
use structopt::StructOpt;
use term_table::{Table, TableStyle, row::Row, table_cell::Alignment, table_cell::TableCell};

// TODO: cache conversions
// TODO: save cached in xdg cache dir?

async fn process_source(source: &SourceConfig) -> Result<Vec<Asset>, Box<dyn Error>> {
    use config::SourceType::*;
    match &source.source_type {
        // TODO: static dispatch
        IBFlex(config) => IBFlexSource::take_snapshot(config).await,
        Hardcoded { assets } => Ok(assets.to_vec()),
    }
}

fn asset_to_money(x: &Asset) -> Money<iso::Currency> {
    match &x.denomination {
        Denomination::Currency { currency } => {
            Money::from_decimal(x.amount, iso::find(currency).unwrap())
        }
        _ => panic!("arg"),
    }
}

enum SourceType {
    Hardcoded,
    IBFlex,
}

struct SourceSnapshot {
    id: String,
    name: String,
    source_type: SourceType,
    snapshot: Vec<Asset>,
}

async fn get_source_snapshots(
    source_configs: &HashMap<String, config::SourceConfig>,
) -> Vec<SourceSnapshot> {
    stream::iter(source_configs)
        .flat_map(|(source_id, source_config)| {
            process_source(source_config)
                .map(move |result| {
                    let assets = result.unwrap_or_else(|_| {
                        panic!("getting result from source {source_id} failed")
                    });
                    info!("{source_id} {} {assets:?}", source_config.name);
                    use config::SourceType::*;
                    SourceSnapshot {
                        id: source_id.clone(),
                        name: source_config.name.clone(),
                        source_type: match source_config.source_type {
                            IBFlex(_) => SourceType::IBFlex,
                            Hardcoded { .. } => SourceType::Hardcoded,
                        },
                        snapshot: assets,
                    }
                })
                .into_stream()
        })
        .collect()
        .await
}

enum ConverterType {
    CurrencyLayer,
    AlphaVantage,
    Fixer,
}

struct ConverterSnapshot {
    id: String,
    converter_type: ConverterType,
    snapshot: Vec<ExchangeRate>,
}

async fn get_converter_snapshots(
    denominations: &[&Denomination],
    converter_configs: &HashMap<String, ConverterConfig>,
    base: &Denomination,
) -> Vec<ConverterSnapshot> {
    use ConverterConfig::*;
    stream::iter(converter_configs)
        .flat_map(|(converter_name, converter_config)| {
            info!("{converter_name}");
            match converter_config {
                AlphaVantage(config) => {
                    // TODO: Err(ParsingError("missing metadata"))
                    // Err(ParsingError("missing exchange rate data"))
                    // this seems to happen on probably too many requests in too
                    // short a time.
                    AlphaVantageConverter::take_snapshot(config, denominations, base)
                }
                Fixer(config) => FixerConverter::take_snapshot(config, denominations, base),
                CurrencyLayer(config) => {
                    CurrencyLayerConverter::take_snapshot(config, denominations, base)
                }
            } // TODO
            .map(move |conversions| {
                let conversions = conversions.unwrap();
                ConverterSnapshot {
                    id: converter_name.clone(),
                    converter_type: match converter_config {
                        AlphaVantage(_) => ConverterType::AlphaVantage,
                        Fixer(_) => ConverterType::Fixer,
                        CurrencyLayer(_) => ConverterType::CurrencyLayer,
                    },
                    snapshot: conversions,
                }
            })
            .into_stream()
        })
        .collect()
        .await
}

fn load_config(xdg_dirs: &xdg::BaseDirectories) -> Result<Config, Box<dyn Error>> {
    let config_path = xdg_dirs.place_config_file("config.yaml")?;
    // TODO: file must exist
    let f = File::open(config_path)?;
    Ok(serde_yaml::from_reader(f).expect("cannot parse config file"))
}

fn get_snapshot_paths(config: &Config) -> Vec<String> {
    let pattern = Path::new(OsStr::new(
        &shellexpand::tilde(&config.dated_json_output).into_owned(),
    ))
    .parent()
    .unwrap()
    .join("*.json");
    let mut paths: Vec<String> = Vec::new();
    for entry in glob(pattern.as_path().to_str().unwrap()).unwrap() {
        match entry {
            Ok(path) => paths.push(path.to_str().unwrap().to_string()),
            Err(e) => panic!("{e:?}"),
        }
    }
    paths
}

async fn model_and_show(
    config: &Config,
    converter_snapshots: &[ConverterSnapshot],
    source_snapshots: &[SourceSnapshot],
) -> Asset {
    let base = Denomination::Currency {
        currency: config.common_currency.clone(),
    };
    let all_conversions: Vec<_> = converter_snapshots
        .iter()
        .flat_map(|snapshot| snapshot.snapshot.clone())
        .collect();
    info!("All conversions: {all_conversions:?}");

    // TODO: deduplicate
    let mut all_assets = HashMap::new();
    for ss in source_snapshots.iter() {
        for asset in ss.snapshot.iter() {
            all_assets
                .entry(asset.denomination.clone())
                .or_insert(Decimal::ZERO);
            *all_assets.get_mut(&asset.denomination).unwrap() += asset.amount;
        }
    }
    info!("All assets: {all_assets:?}");

    let in_common_currency = common_currency::in_common_currency(&all_conversions, &base);
    info!("In common currency: {in_common_currency:?}");

    let mut total_amount = Decimal::ZERO;
    for ss in source_snapshots.iter() {
        info!("{} {}", ss.id, ss.name);

        for asset in ss.snapshot.iter() {
            if let Some(conversion_rate) = in_common_currency.get(&asset.denomination) {
                let amount = asset.amount * conversion_rate;
                info!("{asset:?}: {amount:?} in common currency");
                total_amount += amount;
            } else {
                let denomination = &asset.denomination;
                warn!("{denomination:?} not connected to common currency");
            }
        }
    }

    let total = Asset {
        amount: total_amount,
        denomination: base.clone(),
    };
    info!("Total in common currency: {total:?}");

    if config.cfiresim.is_some() {
        let c = config.cfiresim.as_ref().unwrap();
        // Post to cFIREsim.

        // Add up all sources that are in the portfolio.
        let snapshot_by_id: HashMap<String, &SourceSnapshot> = source_snapshots
            .iter()
            .map(|snapshot| (snapshot.id.clone(), snapshot))
            .collect();

        let add_up_amounts = |account_names: &Vec<String>| -> Decimal {
            let mut total = Decimal::zero();
            for source in account_names {
                info!("source: {source}");
                let snapshot = &snapshot_by_id[source];
                for asset in snapshot.snapshot.iter() {
                    let val = in_common_currency[&asset.denomination] * asset.amount;
                    info!("{asset:?}: {val:?} in common currency");
                    total += val;
                }
            }
            total.floor()
        };

        let portfolio_total = add_up_amounts(&c.portfolio);
        info!("portfolio total: {portfolio_total}");

        let csrf_middleware_token: &str =
            "eFBajFh8XEERVEK6yuI00J4R1qWjonS4xv417X4toibJYzGc220Y36dEcFGcvFZr";
        let mut params: HashMap<String, String> = HashMap::new();
        for (k, v) in &[
            ("csrfmiddlewaretoken", csrf_middleware_token),
            ("data_method", "historical_all"),
            ("single_simulation_year", "1966"),
            ("historical_data_start_year", "1900"),
            ("historical_data_end_year", "1980"),
            ("constant_market_growth", "7.50"),
            ("spending_plan", "inflation_adjusted"),
            ("inflation_type", "cpi"),
            ("inflation_flat_rate", "3.10"),
            ("guyton_exceeds", "20"),
            ("guyton_cut", "10"),
            ("guyton_fall", "20"),
            ("guyton_raise", "10"),
            ("yearly_spending_percent_of_portfolio", "4"),
            ("z_value", "0.50"),
            ("vpw_rate_of_return", "4.30"),
            ("vpw_future_value", "0"),
            ("hebeler_age_at_retirement", "0"),
            ("hebeler_weighted_rmd", "50"),
            ("hebeler_weighted_cpi", "50"),
            ("cape_yield_multiplier", "0.50"),
            ("cape_constant_adjustment", "1.00"),
            ("spending_floor_type", "none"),
            ("spending_floor_value", "0"),
            ("spending_ceiling_type", "none"),
            ("spending_ceiling_value", "0"),
            ("investigate_initial_yearly_spending_threshold", "95"),
            // TODO: split between cash and equities
            ("equities", "100"),
            ("bonds", "0"),
            ("fees", "0.18"),
            ("rebalance_annually", "on"),
            ("gold", "0"),
            ("cash", "0"),
            ("growth_of_cash", "0.25"),
            ("keep_allocation_constant", "on"),
            ("change_allocation_start_year", "2031"),
            ("target_equities", "50"),
            ("target_bonds", "50"),
            ("change_allocation_end_year", "2041"),
            ("target_gold", "0"),
            ("target_cash", "0"),
            ("ss_frequency_toggle", "monthly"),
            ("ss_end_year", "2100"),
            ("ss_spouse_frequency_toggle", "annual"),
            ("ss_spouse_annual_value", "0"),
            ("ss_spouse_start_year", "2036"),
            ("ss_spouse_end_year", "2100"),
            ("form-TOTAL_FORMS", "10"),
            ("form-INITIAL_FORMS", "0"),
            ("form-MIN_NUM_FORMS", "0"),
            ("form-MAX_NUM_FORMS", "1000"),
        ] {
            params.insert(k.to_string(), v.to_string());
        }
        params.insert("retirement_year".to_string(), c.retirement_year.to_string());
        params.insert(
            "retirement_end_year".to_string(),
            c.retirement_end_year.to_string(),
        );
        params.insert(
            "initial_yearly_spending".to_string(),
            c.initial_yearly_spending.to_string(),
        );
        params.insert(
            "ss_start_year".to_string(),
            c.social_security.start_year.to_string(),
        );
        params.insert(
            "ss_annual_value".to_string(),
            c.social_security.monthly_amount.to_string(),
        );
        params.insert("portfolio_value".to_string(), portfolio_total.to_string());

        if !c.adjustment.is_empty() {
            // First adjustment.
            params.insert("form-0-label".to_string(), c.adjustment[0].name.clone());
            params.insert("form-0-adjustment_type".to_string(), "pension".to_string());
            params.insert("form-0-inflation_adjusted".to_string(), "on".to_string());
            params.insert("form-0-inflation_type".to_string(), "cpi".to_string());
            params.insert(
                "form-0-start_year".to_string(),
                c.adjustment[0].year.to_string(),
            );
            let adjustment_total = add_up_amounts(&c.adjustment[0].source);
            info!("adjustment total: {adjustment_total}");
            params.insert(
                "form-0-amount_per_year".to_string(),
                adjustment_total.to_string(),
            );
        } else {
            params.insert("form-0-label".to_string(), "".to_string());
            params.insert("form-0-adjustment_type".to_string(), "income".to_string());
            params.insert("form-0-inflation_adjusted".to_string(), "on".to_string());
            params.insert("form-0-inflation_type".to_string(), "cpi".to_string());
            params.insert("form-0-start_year".to_string(), "2022".to_string());
            params.insert("form-0-end_year".to_string(), "2100".to_string());
            params.insert("form-0-recurring".to_string(), "on".to_string());
        }

        // Remaining adjustments.
        for i in 1..=10 {
            for (k, v) in &[
                ("label", "".to_string()),
                ("amount_per_year", "".to_string()),
                ("adjustment_type", "income".to_string()),
                ("recurring", "on".to_string()),
                ("inflation_adjusted", "on".to_string()),
                ("start_year", "2021".to_string()),
                ("end_year", "2100".to_string()),
                ("inflation_type", "cpi".to_string()),
            ] {
                params.insert(format!("form-{i}-{k}"), v.clone());
            }
        }

        let client = reqwest::Client::new();
        let response = client
            .post("https://www.cfiresim.com/calculator/get_simulation")
            .form(&params)
            .send()
            .await
            .unwrap();
        if !response.status().is_success() {
            println!("{response:#?}");
            println!("{:#?}", response.text().await);
            panic!("error response");
        }
        let v: serde_json::Value = response.json().await.unwrap();
        let v = v.as_object().unwrap();
        // stats
        let fragment = v["stats"].as_str().unwrap();
        let frag = scraper::Html::parse_fragment(fragment);
        let selector =
            scraper::Selector::parse("table.table > tbody > tr > td[scope=row]").unwrap();
        for element in frag.select(&selector) {
            let txt = element.text().collect::<Vec<_>>().join("");
            //"\n                12.34% - Failed 56 of 78 total cycles.\n              "
            println!("{:#?}", txt.trim());
        }

        println!(
            "https://www.cfiresim.com/{}",
            v["tracking_uuid"].as_str().unwrap()
        );
    }

    // TODO(agentydragon): Make configurable
    // How many more years to model for (i.e., remaining lifetime)
    let deadline = dec!(75.0);
    render_table(
        deadline,
        &total,
        &config.modelling,
        &base,
        &in_common_currency,
    );
    total
}

#[tokio::main]
async fn main() {
    env_logger::init();
    let opt = Opt::from_args();
    trace!("Options: {opt:?}");

    let xdg_dirs = xdg::BaseDirectories::with_prefix("worthy");
    let config = load_config(&xdg_dirs).unwrap();
    trace!("Config: {config:?}");

    let now = Utc::now().into();

    use flags::Command::*;
    match opt.command {
        Snapshot => {
            // Collect all assets from all sources.
            // TODO(agentydragon): would be quite nice to do this via futures...
            let source_snapshots = get_source_snapshots(&config.source_config).await;

            // TODO: deduplicate
            let mut all_assets = HashMap::new();
            for ss in source_snapshots.iter() {
                for asset in ss.snapshot.iter() {
                    all_assets
                        .entry(asset.denomination.clone())
                        .or_insert(Decimal::ZERO);
                    *all_assets.get_mut(&asset.denomination).unwrap() += asset.amount;
                }
            }
            info!("All assets: {all_assets:?}");

            // TODO: check it exists
            let base = Denomination::Currency {
                currency: config.common_currency.clone(),
            };

            let converter_snapshots = get_converter_snapshots(
                &all_assets.keys().collect::<Vec<_>>(),
                &config.converter_config,
                &base,
            )
            .await;

            let total = model_and_show(&config, &converter_snapshots, &source_snapshots).await;

            // Save JSON snapshot.
            let json_snapshot = json_output::Snapshot {
                // TODO(agentydragon): should be shared
                timestamp: now,
                source_snapshot: source_snapshots
                    .iter()
                    .map(source_snapshot_to_json)
                    .collect(),
                converter_snapshots: converter_snapshots
                    .iter()
                    .map(converter_snapshot_to_json)
                    .collect(),
                total: asset_to_json(&total),
            };
            let s = serde_json::to_string_pretty(&json_snapshot).unwrap();

            let output_path =
                shellexpand::tilde(&config.dated_json_output).replace("%s", &now.to_rfc3339());

            {
                let mut file = File::create(&output_path).unwrap();
                file.write_all(s.bytes().collect::<Vec<_>>().as_slice())
                    .unwrap();
            }
        }
        ModelLastSnapshot => {
            let paths = get_snapshot_paths(&config);
            let loaded_path = paths.iter().max().unwrap();
            let file = File::open(loaded_path).unwrap();
            let snapshot: json_output::Snapshot = serde_json::from_reader(file).unwrap();

            let converter_snapshots: Vec<ConverterSnapshot> = snapshot
                .converter_snapshots
                .iter()
                .map(converter_snapshot_from_json)
                .collect();
            let source_snapshots: Vec<SourceSnapshot> = snapshot
                .source_snapshot
                .iter()
                .map(source_snapshot_from_json)
                .collect();
            let _total = model_and_show(&config, &converter_snapshots, &source_snapshots).await;
        }
        Csv => {
            let paths = get_snapshot_paths(&config);

            let csv_path = shellexpand::tilde(&config.csv_output)
                .into_owned()
                .replace("%s", &now.to_rfc3339());
            let mut wtr = csv::Writer::from_writer(File::create(&csv_path).unwrap());
            wtr.write_record(["Timestamp", "Total"]).unwrap();
            for path in paths {
                let file = File::open(&path).unwrap();
                let snapshot: json_output::Snapshot =
                    serde_json::from_reader(file).unwrap_or_else(|error: serde_json::Error| {
                        panic!("error parsing {path}: {error}")
                    });

                wtr.write_record(&[
                    snapshot.timestamp.to_rfc3339(),
                    snapshot.total.amount.to_string(),
                ])
                .unwrap();
            }

            println!("Written: {csv_path}");
        }
        Server => panic!("TODO"),
    }
}

fn converter_snapshot_to_json(
    converter_snapshot: &ConverterSnapshot,
) -> json_output::ConverterSnapshot {
    json_output::ConverterSnapshot {
        id: converter_snapshot.id.clone(),
        converter_type: match converter_snapshot.converter_type {
            ConverterType::CurrencyLayer => json_output::ConverterType::CurrencyLayer,
            ConverterType::AlphaVantage => json_output::ConverterType::AlphaVantage,
            ConverterType::Fixer => json_output::ConverterType::Fixer,
        },
        snapshot: converter_snapshot
            .snapshot
            .iter()
            .map(exchange_rate_to_json)
            .collect(),
    }
}

fn converter_snapshot_from_json(
    converter_snapshot: &json_output::ConverterSnapshot,
) -> ConverterSnapshot {
    ConverterSnapshot {
        id: converter_snapshot.id.clone(),
        converter_type: match converter_snapshot.converter_type {
            json_output::ConverterType::CurrencyLayer => ConverterType::CurrencyLayer,
            json_output::ConverterType::AlphaVantage => ConverterType::AlphaVantage,
            json_output::ConverterType::Fixer => ConverterType::Fixer,
        },
        snapshot: converter_snapshot
            .snapshot
            .iter()
            .map(exchange_rate_from_json)
            .collect(),
    }
}

fn exchange_rate_from_json(c: &json_output::Conversion) -> ExchangeRate {
    let json_output::Conversion {
        source,
        target,
        target_per_source,
    } = c;
    ExchangeRate {
        from: denomination_from_json(source),
        to: denomination_from_json(target),
        rate: *target_per_source,
    }
}

fn exchange_rate_to_json(exchange_rate: &ExchangeRate) -> json_output::Conversion {
    let ExchangeRate { from, to, rate } = exchange_rate;
    json_output::Conversion {
        source: denomination_to_json(from),
        target: denomination_to_json(to),
        target_per_source: *rate,
    }
}

fn source_snapshot_to_json(source_snapshot: &SourceSnapshot) -> json_output::SourceSnapshot {
    json_output::SourceSnapshot {
        id: source_snapshot.id.clone(),
        name: source_snapshot.name.clone(),
        source_type: match source_snapshot.source_type {
            SourceType::Hardcoded => json_output::SourceType::Hardcoded,
            SourceType::IBFlex => json_output::SourceType::IBFlex,
        },
        snapshot: source_snapshot.snapshot.iter().map(asset_to_json).collect(),
    }
}

fn source_snapshot_from_json(json_snapshot: &json_output::SourceSnapshot) -> SourceSnapshot {
    SourceSnapshot {
        id: json_snapshot.id.clone(),
        name: json_snapshot.name.clone(),
        source_type: match json_snapshot.source_type {
            json_output::SourceType::Hardcoded => SourceType::Hardcoded,
            json_output::SourceType::IBFlex => SourceType::IBFlex,
        },
        snapshot: json_snapshot.snapshot.iter().map(asset_from_json).collect(),
    }
}

fn denomination_to_json(denomination: &Denomination) -> json_output::Denomination {
    match denomination {
        Denomination::Currency { currency } => json_output::Denomination::Currency {
            symbol: currency.clone(),
        },
        Denomination::Cryptocurrency { symbol } => json_output::Denomination::Cryptocurrency {
            symbol: symbol.clone(),
        },
        Denomination::Stock { stock } => json_output::Denomination::Stock {
            symbol: stock.clone(),
        },
    }
}

fn denomination_from_json(denomination: &json_output::Denomination) -> Denomination {
    match denomination {
        json_output::Denomination::Currency { symbol } => Denomination::Currency {
            currency: symbol.clone(),
        },
        json_output::Denomination::Cryptocurrency { symbol } => Denomination::Cryptocurrency {
            symbol: symbol.clone(),
        },
        json_output::Denomination::Stock { symbol } => Denomination::Stock {
            stock: symbol.clone(),
        },
    }
}

fn asset_to_json(asset: &Asset) -> json_output::Asset {
    json_output::Asset {
        denomination: denomination_to_json(&asset.denomination),
        amount: asset.amount,
    }
}

fn asset_from_json(asset: &json_output::Asset) -> Asset {
    Asset {
        denomination: denomination_from_json(&asset.denomination),
        amount: asset.amount,
    }
}

fn render_table(
    deadline: Decimal,
    total: &Asset,
    modelling: &config::ModellingConfig,
    base: &Denomination,
    in_common_currency: &HashMap<Denomination, Decimal>,
) {
    let mut table = Table::new();

    table.max_column_width = 40;
    table.style = TableStyle::extended();

    table.add_row(Row::new(vec![
        TableCell::builder(format!(
            "\u{2211} {}\nHorizon: {} years",
            asset_to_money(total),
            deadline
        ))
        .col_span(1 + modelling.yearly_yields.len())
        .alignment(Alignment::Center)
        .build(),
    ]));

    let mut header = vec![TableCell::new(
        "Yearly yield \u{2192}\nMonthly goal \u{2193}".to_string(),
    )];
    // \u2211 = N-ary summation
    for yld in &modelling.yearly_yields {
        header.push(TableCell::new(format!("{:.2}%", yld * dec!(100),)));
    }
    table.add_row(Row::new(header));

    let mut perpetuals = vec![TableCell::new("Perpetuals".to_string())];
    let denominations: HashSet<Denomination> = modelling
        .monthly_targets
        .iter()
        .map(|asset| asset.denomination.clone())
        .collect();
    for yearly_yield in &modelling.yearly_yields {
        let mut perps = Vec::new();
        for denomination in denominations.iter() {
            let perpetual = get_perpetual(total, *yearly_yield, in_common_currency, denomination);
            perps.push(format!("{}", asset_to_money(&perpetual)));
        }
        perpetuals.push(TableCell::new(perps.join("\n")));
    }

    table.add_row(Row::new(perpetuals));

    for goal in &modelling.monthly_targets {
        let mut results = Vec::new();
        results.push(TableCell::new(format!("{}", asset_to_money(goal))));

        for yearly_yield in &modelling.yearly_yields {
            let result = model_fi_info(
                total,
                in_common_currency,
                *yearly_yield,
                goal.clone(),
                &modelling.monthly_saving,
                deadline,
            );
            use model_rs::State::*;
            results.push(TableCell::new(match result.model_fi_info.state {
                NotReached { .. } => {
                    // 2693 = unicode anchor
                    // 1F4B0 = bag with money
                    format!(
                        "💰 ≥{}\n{}",
                        asset_to_money(&Asset {
                            amount: result.model_fi_info.need_to_last_until_deadline,
                            denomination: base.clone()
                        }),
                        result.model_fi_info.lasts_until_short_string()
                    )
                }
                Reached { .. } => result.model_fi_info.lasts_until_short_string(),
            }));
        }
        table.add_row(Row::new(results));
    }
    print!("{}", table.render());
}

struct FiInfo {
    model_fi_info: model_rs::FiInfo,
}

fn get_perpetual(
    total: &Asset,
    yearly_yield: Decimal,
    common_prices: &HashMap<Denomination, Decimal>,
    denomination: &Denomination,
) -> Asset {
    let amount = (total.amount * yearly_yield / dec!(12)) / common_prices[denomination];
    Asset {
        amount,
        denomination: denomination.clone(),
    }
}

// Yearly yield: 0.03 means assumed yearly yield of 3%.
fn model_fi_info(
    total: &Asset,
    common_prices: &HashMap<Denomination, Decimal>,
    yearly_yield: Decimal,
    monthly_goal: Asset,
    monthly_saving: &Asset,
    deadline: Decimal,
) -> FiInfo {
    // TODO(agentydragon): make the monthly spend limited to the deadline, not
    // perpetual
    //
    // does not seem to work so well - sometimes is smaller than perpetual,
    // which it should not be:
    //
    //i_prime := math.Log(1 + yearly_yield)
    //f := math.Pow(1+yearly_yield, deadline)
    //projectedInCommon := (total.Amount * i_prime * f / (f - 1)) / 12
    //projectedMonthlySpend :=
    //	makeCurrency(monthly_goal.Denomination.Symbol, projectedInCommon/common_prices[monthly_goal.Denomination])
    //fmt.Printf("yearly yield %.2g%%, monthly goal %s, projected monthly spend %s, perpetual %s\n", yearly_yield*100.0, monthly_goal,
    //	projectedMonthlySpend)

    let to_common = |x: &Asset| -> Decimal { common_prices[&x.denomination] * x.amount };
    FiInfo {
        model_fi_info: model_rs::model_fi_info(
            to_common(total),
            yearly_yield,
            to_common(&monthly_goal),
            to_common(monthly_saving),
            deadline,
        ),
    }
}
