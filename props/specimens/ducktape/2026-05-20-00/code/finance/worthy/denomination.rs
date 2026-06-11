use serde::Deserialize;

#[derive(Deserialize, PartialEq, Eq, Hash, Debug, Clone)]
#[serde(untagged)]
pub enum Denomination {
    Currency {
        /// ISO 4217 code
        // TODO: rename to iso code; but then, it's actually the ISO referencing code...
        // but serializated as currency, needed for the "untagged" enum.
        currency: String,
    },
    // TODO: make this not serializable maybe?
    Cryptocurrency {
        symbol: String,
    },
    Stock {
        stock: String,
    },
}
