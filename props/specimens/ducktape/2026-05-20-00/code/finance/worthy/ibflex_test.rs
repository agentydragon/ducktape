use ibflex::{
    AssetCategory::Stock,
    FlexQueryResponse, FlexQuerySuccess, FlexStatement, FlexStatementResponse, FlexStatements,
    LevelOfDetail::Summary,
    OpenPosition, OpenPositions,
    Period::LastBusinessDay,
    Side::Long,
    Status::{Fail, Success},
    parse_flex_statement_response,
};
use rust_decimal::Decimal;
use url::Url;

#[test]
fn flex_statement_response_success() {
    let xml = "<FlexStatementResponse timestamp='16 February, 2021 04:50 PM EST'>
<Status>Success</Status>
<ReferenceCode>4672968268</ReferenceCode>
<Url>https://gdcdyn.interactivebrokers.com/Universal/servlet/FlexStatementService.GetStatement</Url>
</FlexStatementResponse>";
    assert_eq!(
        parse_flex_statement_response(xml).unwrap(),
        FlexStatementResponse {
            status: Success,
            timestamp: "16 February, 2021 04:50 PM EST".to_string(),
            url: Some(Url::parse("https://gdcdyn.interactivebrokers.com/Universal/servlet/FlexStatementService.GetStatement").unwrap()),
            reference_code: Some("4672968268".to_string()),
            error_code: None,
            error_message: None,
        }
    );
}

#[test]
fn flex_statement_response_error_invalid() {
    let xml = "<FlexStatementResponse timestamp='16 February, 2021 05:16 PM EST'>
<Status>Fail</Status>
<ErrorCode>1020</ErrorCode>
<ErrorMessage>Invalid request or unable to validate request.</ErrorMessage>
</FlexStatementResponse>";
    assert_eq!(
        parse_flex_statement_response(xml).unwrap(),
        FlexStatementResponse {
            status: Fail,
            timestamp: "16 February, 2021 05:16 PM EST".to_string(),
            url: None,
            reference_code: None,
            error_code: Some(1020),
            error_message: Some("Invalid request or unable to validate request.".to_string()),
        }
    );
}

/// Test with highly pruned actual response.
#[test]
fn flex_query_response_valid() {
    let xml = r#"<FlexQueryResponse queryName="TestFlexQuery" type="AF">
<FlexStatements count="1">
<FlexStatement accountId="U99999" fromDate="20210215" toDate="20210215" period="LastBusinessDay" whenGenerated="20210216;175211">
<AccountInformation accountId="U99999" currency="CHF" acctAlias="" name="John Doe" accountType="Individual" customerType="Individual" accountCapabilities="Cash" tradingPermissions="Stocks,Warrants,Forex" masterName="" />
<CashReport>
<CashReportCurrency accountId="U99999" acctAlias="" currency="BASE_SUMMARY" levelOfDetail="BaseCurrency" fromDate="20210215" toDate="20210215" />
<CashReportCurrency accountId="U99999" acctAlias="" currency="CHF" levelOfDetail="Currency" fromDate="20210215" toDate="20210215" />
<CashReportCurrency accountId="U99999" acctAlias="" currency="USD" levelOfDetail="Currency" fromDate="20210215" toDate="20210215" />
</CashReport>
<StmtFunds>
</StmtFunds>
<OpenPositions>
<OpenPosition accountId="U99999" acctAlias="" currency="USD" fxRateToBase="0.8903" assetCategory="STK" symbol="ABCD" description="Abcd Stock" conid="11111" securityID="US12345" securityIDType="ISIN" cusip="AA111" isin="US12345" listingExchange="NASDAQ" issuer="" multiplier="1" strike="" expiry="" putCall="" principalAdjustFactor="" reportDate="20210215" position="1111" markPrice="11.11" positionValue="123" openPrice="1.1" costBasisPrice="11.1" costBasisMoney="9999" percentOfNAV="80.5" fifoPnlUnrealized="111" side="Long" levelOfDetail="SUMMARY" />
<OpenPosition accountId="U99999" acctAlias="" currency="USD" fxRateToBase="0.8903" assetCategory="STK" symbol="EFGH" description="Efgh Stock" conid="22222" securityID="US12346" securityIDType="ISIN" cusip="BB222" isin="US12346" listingExchange="ARCA" issuer="" multiplier="1" strike="" expiry="" putCall="" principalAdjustFactor="" reportDate="20210215" position="1112" markPrice="22.22" positionValue="456" openPrice="1.2" costBasisPrice="11.1" costBasisMoney="1111" percentOfNAV="19.5" fifoPnlUnrealized="222" side="Long" levelOfDetail="SUMMARY" />
</OpenPositions><NetStockPositionSummary>
</NetStockPositionSummary>
</FlexStatement>
</FlexStatements>
</FlexQueryResponse>"#;
    assert_eq!(
        ibflex::parse_flex_query_response(xml).unwrap(),
        FlexQueryResponse::Success(FlexQuerySuccess {
            response_type: "AF".to_string(),
            flex_statements: FlexStatements {
                count: 1,
                flex_statements: vec![FlexStatement {
                    open_positions: OpenPositions {
                        open_position: Some(vec![
                            OpenPosition {
                                account_id: "U99999".to_string(),
                                acct_alias: "".to_string(),
                                currency: "USD".to_string(),
                                asset_category: Stock,
                                symbol: "ABCD".to_string(),
                                description: "Abcd Stock".to_string(),
                                multiplier: Decimal::new(1, 0),
                                fx_rate_to_base: Decimal::new(8903, 4),
                                mark_price: Decimal::new(1111, 2),
                                position: Decimal::new(1111, 0),
                                side: Long,
                                level_of_detail: Summary,
                                issuer: "".to_string(),
                                expiry: "".to_string(),
                                put_call: "".to_string(),
                                isin: "US12345".to_string(),
                            },
                            OpenPosition {
                                account_id: "U99999".to_string(),
                                acct_alias: "".to_string(),
                                currency: "USD".to_string(),
                                asset_category: Stock,
                                symbol: "EFGH".to_string(),
                                description: "Efgh Stock".to_string(),
                                multiplier: Decimal::new(1, 0),
                                fx_rate_to_base: Decimal::new(8903, 4),
                                mark_price: Decimal::new(2222, 2),
                                position: Decimal::new(1112, 0),
                                side: Long,
                                level_of_detail: Summary,
                                issuer: "".to_string(),
                                expiry: "".to_string(),
                                put_call: "".to_string(),
                                isin: "US12346".to_string(),
                            }
                        ])
                    },
                    account_id: "U99999".to_string(),
                    from_date: "20210215".to_string(),
                    to_date: "20210215".to_string(),
                    period: LastBusinessDay,
                    when_generated: "20210216;175211".to_string()
                }]
            },
        })
    );
}
