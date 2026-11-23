mod differential;

use chrono::Duration;
use chrono::prelude::*;
use log::info;
use rust_decimal::prelude::*;
use rust_decimal_macros::*;

fn hack_pow(a: Decimal, b: Decimal) -> Decimal {
    Decimal::from_f64(a.to_f64().unwrap().powf(b.to_f64().unwrap())).unwrap()
}

fn decimal_log(x: Decimal) -> Decimal {
    info!("ln({x})");
    Decimal::from_f64(x.to_f64().unwrap().ln()).unwrap()
}

/// How much money we'd need to get if we want to
fn deadline_target(yearly_yield: Decimal, monthly_goal: Decimal, deadline: Decimal) -> Decimal {
    ((monthly_goal * dec!(12)) / decimal_log(dec!(1) + yearly_yield))
        * (dec!(1) - hack_pow(dec!(1) + yearly_yield, -deadline))
}

pub enum State {
    Reached {
        overreach_percentage: Decimal,
    },
    NotReached {
        durability: Duration,
        until_saved_up: Duration,

        lasts_until: DateTime<Utc>,
        projected_until_saved: DateTime<Utc>,
    },
}

pub struct FiInfo {
    pub deadline: Decimal,
    pub need_to_last_until_deadline: Decimal,
    pub total: Decimal,
    pub monthly_saving: Decimal,
    pub state: State,
}

impl FiInfo {
    pub fn lasts_until_short_string(&self) -> String {
        match self.state {
            State::Reached {
                overreach_percentage,
            } => {
                format!("{overreach_percentage:.0}% ✓")
            }
            State::NotReached {
                lasts_until,
                projected_until_saved,
                ..
            } => {
                // 2912 = upwards arrow to bar
                // 2913 = downwards arrow to bar
                format!(
                    "\u{2912} {}\n\u{2913} {}",
                    projected_until_saved.format("%Y-%m-%d"),
                    lasts_until.format("%Y-%m-%d")
                )
            }
        }
    }
}

fn years_duration(years: Decimal) -> Duration {
    Duration::seconds((years.to_f64().unwrap() * 24_f64 * 60_f64 * 60_f64 * 365.24).round() as i64)
}

/// Yearly yield: 0.03 means assumed yearly yield of 3%.
pub fn model_fi_info(
    total: Decimal,
    yearly_yield: Decimal,
    monthly_goal: Decimal,
    monthly_saving: Decimal,
    deadline: Decimal,
) -> FiInfo {
    let now = Utc::now();

    let target = deadline_target(yearly_yield, monthly_goal, deadline);
    FiInfo {
        total,
        deadline,
        need_to_last_until_deadline: target,
        monthly_saving,
        state: if target < total {
            State::Reached {
                overreach_percentage: (total / target) * dec!(100),
            }
        } else {
            info!("We need {target}, we have {total}");
            let durability =
                differential::get_investment_durability(total, yearly_yield, monthly_goal);
            let need_years =
                differential::years_until_saved_up_exp(total, yearly_yield, target, monthly_saving);
            let need_years = years_duration(need_years);
            let durability = years_duration(durability);

            State::NotReached {
                durability,
                until_saved_up: need_years,
                lasts_until: now + durability,
                projected_until_saved: now + need_years,
            }
        },
    }
}
