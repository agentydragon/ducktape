use log::{error, trace};
use reqwest::StatusCode;
use rust_decimal::Decimal;
use serde::Deserialize;
use std::{
    collections::HashSet,
    error::Error,
    fmt,
    fmt::{Display, Formatter},
};
use tokio::time::{Duration, sleep};
use url::Url;

#[derive(Debug, Deserialize, PartialEq)]
pub enum Status {
    Success,
    Fail,
}

mod status_deserializer {
    use super::Status;
    use serde::{Deserialize, Deserializer};

    pub fn deserialize<'de, D>(deserializer: D) -> Result<Status, D::Error>
    where
        D: Deserializer<'de>,
    {
        let s = String::deserialize(deserializer)?;
        match s.as_str() {
            "Success" => Ok(Status::Success),
            "Fail" => Ok(Status::Fail),
            _ => Err(serde::de::Error::custom(format!("unknown status: {s}"))),
        }
    }
}

#[derive(Debug, Deserialize, PartialEq)]
#[serde(rename_all = "PascalCase")]
pub struct FlexStatementResponse {
    // TODO: https://serde.rs/custom-date-format.html
    // 16 February, 2021 04:50 PM EST
    #[serde(rename = "Status")]
    #[serde(with = "status_deserializer")]
    pub status: Status,
    #[serde(rename = "@timestamp")]
    pub timestamp: String,

    // if status == Success:
    #[serde(rename = "ReferenceCode")]
    pub reference_code: Option<String>,
    #[serde(rename = "Url")]
    pub url: Option<Url>,

    // if status error:
    #[serde(rename = "ErrorCode")]
    pub error_code: Option<i32>,
    #[serde(rename = "ErrorMessage")]
    pub error_message: Option<String>,
}

#[derive(Debug, Deserialize, PartialEq)]
pub enum AssetCategory {
    #[serde(rename = "STK")]
    Stock,
}
/*
if openPosition.Multiplier != "1" {
    return nil, errors.New("multiplier not supported")
}
if openPosition.AssetCategory != "STK" {
    return nil, errors.New("only stocks supported")
}
if openPosition.PutCall != "" || openPosition.Issuer != "" || openPosition.Expiry != "" ||
    openPosition.LevelOfDetail != "SUMMARY" {
    return nil, errors.New("unexpected fields populated")
}
if openPosition.Side != "Long" {
    return nil, errors.New("only Long positions supported")
}

if existing, ok := exchangeRates[openPosition.Currency]; ok {
    if existing != openPosition.FxRateToBase {
        return nil, errors.New("inconsistent rate for currency")
    }
} else {
    exchangeRates[openPosition.Currency] = openPosition.FxRateToBase
}
self.logger.Println(openPosition.Symbol, openPosition.Description,
    // Position:"6",
    openPosition.Position,
    // market price per unit
    openPosition.MarkPrice,
    // currency of market price
    openPosition.Currency)
    */

#[derive(Debug, Deserialize, PartialEq)]
pub enum Side {
    Long,
}

#[derive(Debug, Deserialize, PartialEq)]
pub struct OpenPosition {
    #[serde(rename = "@accountId")]
    pub account_id: String,
    #[serde(rename = "@acctAlias")]
    pub acct_alias: String,
    #[serde(rename = "@currency")]
    pub currency: String,
    #[serde(rename = "@assetCategory")]
    pub asset_category: AssetCategory, /*STK*/
    #[serde(rename = "@symbol")]
    pub symbol: String, /* TSLA*/
    #[serde(rename = "@description")]
    pub description: String, /* TSLA*/
    #[serde(rename = "@multiplier")]
    pub multiplier: Decimal, /* 1*/
    #[serde(rename = "@fxRateToBase")]
    pub fx_rate_to_base: Decimal,
    //Conid             string `xml:"conid,attr"`
    //SecurityID        string `xml:"securityID,attr"`
    //SecurityIDType    string `xml:"securityIDType,attr"`
    #[serde(rename = "@isin")]
    pub isin: String,
    //Cusip             string `xml:"cusip,attr"`
    #[serde(rename = "@markPrice")]
    pub mark_price: Decimal,
    //ListingExchange   string `xml:"listingExchange,attr"`
    #[serde(rename = "@position")]
    pub position: Decimal,
    #[serde(rename = "@side")]
    pub side: Side,
    //ReportDate        string `xml:"reportDate,attr"`
    #[serde(rename = "@levelOfDetail")]
    pub level_of_detail: LevelOfDetail,
    //PositionValue     string `xml:"positionValue,attr"`
    //OpenPrice         string `xml:"openPrice,attr"`
    //PercentOfNAV      string `xml:"percentOfNAV,attr"`
    //CostBasisPrice    string `xml:"costBasisPrice,attr"`
    //CostBasisMoney    string `xml:"costBasisMoney,attr"`
    //FifoPnlUnrealized string `xml:"fifoPnlUnrealized,attr"`
    #[serde(rename = "@issuer")]
    pub issuer: String,
    #[serde(rename = "@expiry")]
    pub expiry: String,
    #[serde(rename = "@putCall")]
    pub put_call: String,
}

#[derive(Debug, Deserialize, PartialEq)]
pub struct OpenPositions {
    #[serde(rename = "OpenPosition")]
    pub open_position: Option<Vec<OpenPosition>>,
}

#[derive(Debug, Deserialize, PartialEq)]
pub enum LevelOfDetail {
    #[serde(rename = "SUMMARY")]
    Summary,
}

#[derive(Debug, Deserialize, PartialEq)]
pub struct FlexStatement {
    #[serde(rename = "OpenPositions")]
    pub open_positions: OpenPositions,
    #[serde(rename = "@accountId")]
    pub account_id: String,
    #[serde(rename = "@fromDate")]
    pub from_date: String,
    #[serde(rename = "@toDate")]
    pub to_date: String,
    #[serde(rename = "@period")]
    pub period: Period,
    #[serde(rename = "@whenGenerated")]
    pub when_generated: String,
}

#[derive(Debug, Deserialize, PartialEq)]
pub struct FlexStatements {
    #[serde(rename = "@count")]
    pub count: i32,
    #[serde(rename = "FlexStatement")]
    pub flex_statements: Vec<FlexStatement>,
}

#[derive(Debug, Deserialize, PartialEq)]
pub enum Period {
    #[serde(rename = "LastBusinessDay")]
    LastBusinessDay,
}

#[derive(Debug, Deserialize, PartialEq)]
#[serde(rename_all = "PascalCase")]
pub struct FlexQueryResponseXml {
    // present on error
    pub error_code: Option<i32>,
    pub error_message: Option<String>,

    #[serde(rename = "@type")]
    // present on success
    pub response_type: Option<String>,
    pub flex_statements: Option<FlexStatements>,
}

#[derive(Debug, PartialEq)]
pub struct FlexQuerySuccess {
    pub response_type: String,
    pub flex_statements: FlexStatements,
}

#[derive(Debug, PartialEq)]
pub enum FlexQueryResponse {
    Error(FlexError),
    Success(FlexQuerySuccess),
}

const FLEX_API_VERSION: i32 = 3;
const ENDPOINT: &str =
    "https://gdcdyn.interactivebrokers.com/Universal/servlet/FlexStatementService.SendRequest";

pub fn parse_flex_statement_response(
    text: &str,
) -> Result<FlexStatementResponse, serde_xml_rs::Error> {
    serde_xml_rs::from_str(text)
}

#[derive(Debug, PartialEq)]
enum IBFlexError {
    ParseError,
    HttpError { status: StatusCode },
}

impl Error for IBFlexError {}

impl Display for IBFlexError {
    fn fmt(&self, f: &mut Formatter) -> fmt::Result {
        write!(f, "Parse error")
    }
}

pub fn parse_flex_query_response(text: &str) -> Result<FlexQueryResponse, Box<dyn Error>> {
    let FlexQueryResponseXml {
        error_code,
        error_message,
        response_type,
        flex_statements,
    } = serde_xml_rs::from_str(text)?;
    match (error_code, error_message, response_type, flex_statements) {
        (Some(code), Some(message), None, None) => {
            Ok(FlexQueryResponse::Error(FlexError { code, message }))
        }
        (None, None, Some(response_type), Some(flex_statements)) => {
            Ok(FlexQueryResponse::Success(FlexQuerySuccess {
                response_type,
                flex_statements,
            }))
        }
        _ => Err(IBFlexError::ParseError.into()),
    }
}

fn check_http_ok(r: &reqwest::Response) -> Result<(), IBFlexError> {
    match r.status() {
        StatusCode::OK => Ok(()),
        status => Err(IBFlexError::HttpError { status }),
    }
}

async fn run_flex_query2(
    token: &str,
    query_id: &str,
) -> Result<FlexStatementResponse, Box<dyn Error>> {
    let mut url = Url::parse(ENDPOINT)?;
    url.query_pairs_mut()
        .clear()
        .append_pair("t", token)
        .append_pair("q", query_id)
        .append_pair("v", &FLEX_API_VERSION.to_string());

    let response = reqwest::get(url).await?;
    check_http_ok(&response)?;
    trace!("{response:?}");
    let text = response.text().await?;
    trace!("{text:?}");
    Ok(serde_xml_rs::from_str(&text).unwrap())
}

async fn fetch_flex_query_result(
    token: &str,
    reference_code: &str,
    url: &Url,
) -> Result<FlexQueryResponse, Box<dyn Error>> {
    let mut url = url.clone();
    url.query_pairs_mut()
        .clear()
        .append_pair("t", token)
        .append_pair("q", reference_code)
        .append_pair("v", &FLEX_API_VERSION.to_string());

    let response = reqwest::get(url).await?;
    check_http_ok(&response)?;
    let text = response.text().await?;
    trace!("{text:?}");
    parse_flex_query_response(&text)
}

#[derive(Debug, PartialEq, Eq)]
pub struct FlexError {
    code: i32,
    message: String,
}

impl Display for FlexError {
    fn fmt(&self, f: &mut Formatter) -> fmt::Result {
        write!(f, "Flex error: {} {}", self.code, self.message)
    }
}
impl Error for FlexError {}

impl FlexError {
    fn is_retriable(&self) -> bool {
        let retriable_codes: HashSet<i32> = [1009, 1019, 1004].iter().cloned().collect();
        retriable_codes.contains(&self.code) && self.message.contains("Please try again shortly")
    }
}

pub async fn run_flex_query(
    token: &str,
    query_id: &str,
) -> Result<FlexQuerySuccess, Box<dyn Error>> {
    let response = run_flex_query2(token, query_id).await?;
    trace!("Response: {response:?}");
    // TODO: Error response: code=1004 message=Statement is incomplete at this time. Please try again shortly.
    if response.status != Status::Success {
        let code = response.error_code.as_ref().unwrap();
        let message = response.error_message.as_ref().unwrap();
        error!("Error response: code={code} message={message}");
        return Err(Box::new(FlexError {
            code: response.error_code.unwrap(),
            message: response.error_message.unwrap(),
        }));
    }
    // TODO: error if: if response.error_code.is_some() {
    // TODO: error if: }
    // "<FlexStatementResponse timestamp=\'13 March, 2021 02:07 PM EST\'>
    // <Status>Fail</Status>
    // <ErrorCode>1014</ErrorCode>
    // <ErrorMessage>Query is invalid.</ErrorMessage>
    // </FlexStatementResponse>\n"
    // TODO: raise FlexError otherwise
    // FlexStatementResponse { status: Fail, timestamp: "08 September, 2021 06:17 PM EDT", reference_code: None, url: None, error_code: Some(1003), error_message: Some("Statement is not available.") }
    let url = response.url.unwrap();
    let reference_code = response.reference_code.unwrap();

    let mut retries = 0;
    'attempt: loop {
        let r = fetch_flex_query_result(token, &reference_code, &url).await?;

        match r {
            FlexQueryResponse::Error(error) => {
                if !error.is_retriable() {
                    error!("unretriable error");
                    return Err(error.into());
                }
                if retries >= 5 {
                    error!("out of retries");
                    return Err(error.into());
                }
                // Retry
                retries += 1;
                sleep(Duration::from_millis(1000)).await;
                continue 'attempt;
            }
            FlexQueryResponse::Success(success) => return Ok(success),
        }
    }
}
