# Property taxes by location

Reference facts for the places Augur has to price housing in, with their sources. Pin
property-tax cases to these (refreshed to the selected tax year) rather than to the
engine's own numbers.

Today `sim/property_tax.py` bills purchase price × one ad-valorem rate plus one flat annual
special assessment, in equal monthly twelfths, both fixed for the whole horizon.

## California (both locations)

- **Proposition 13:** the ad-valorem base is 1% of assessed value. Assessed value is reset
  to the purchase price on a change of ownership and afterwards grows at most 2% a year.
  Voter-approved bond rates are added on top of the 1% and are not capped.
- **Special taxes:** Mello-Roos / Community Facilities District (CFD) taxes and parcel taxes
  are flat per-parcel charges, not ad valorem and not capped by Proposition 13. A CFD's rate
  formula typically escalates its charge by up to 2% a year, and the district has a stated
  term.

## San Francisco

- **Ad-valorem rate:** 1% base plus about 0.18% of voter-approved city and county bond rates,
  about 1.18% in total.
- **Special taxes:** standard parcels carry no Mello-Roos / CFD taxes.

## Vallejo: Mare Island

Mare Island is a former U.S. Navy base on Vallejo's western edge. It was transferred to the
City of Vallejo in 1996 and redeveloped into residential, commercial and light-industrial
parcels.

- **Ad-valorem rate:** 1% base plus about 0.15% of Solano County, Vallejo and school-district
  bond rates, about 1.15% in total.
- **Special taxes:** three overlapping City of Vallejo CFDs appear on residential bills as
  flat special taxes:
  - CFD 2002-1, Mare Island Facilities: roads, sewer, and remediation tied to the Navy land
    transfer.
  - CFD 2005-1A, Mare Island Services: police and fire, charged from population served and
    building square footage.
  - CFD 2005-1B: a secondary district with little public detail.
- **Amount:** CBS San Francisco (2024) reports residential owners pay roughly $2,300 a year
  more than mainland Vallejo, with about 322 single-family parcels contributing over $3M a
  year between them (commercial parcels carry the larger share). Each residential parcel's
  charge is capped at a flat maximum.
- **Term:** the districts are designed to sunset as development completes.

Sources:

- <https://www.cbsnews.com/sanfrancisco/news/vallejo-mare-island-homeowners-seek-to-shed-special-tax-theyve-uselessly-paid-for-years/>
- <https://www.vallejosun.com/mare-island-property-owners-ask-vallejo-council-to-end-special-tax-for-police-and-fire-services/>
- <https://www.vallejosun.com/mare-island-residents-request-state-audit-of-vallejos-special-tax-spending/>
- <https://www.cityofvallejo.net/common/pages/DisplayFile.aspx?itemId=19274617>
