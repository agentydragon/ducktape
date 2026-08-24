"""
Genomic Analysis with Database Integration
Uses Pydantic for data validation and efficient batch queries
"""

import argparse
import json
import sqlite3
import time
import traceback
from collections import defaultdict
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

import pandas as pd
import requests
from jinja2 import Template
from pydantic import BaseModel, ConfigDict, Field, field_validator


class ClinicalSignificance(StrEnum):
    PATHOGENIC = "pathogenic"
    LIKELY_PATHOGENIC = "likely_pathogenic"
    UNCERTAIN = "uncertain_significance"
    LIKELY_BENIGN = "likely_benign"
    BENIGN = "benign"


class Variant(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    rsid: str
    chromosome: str
    position: int
    genotype: str


class CADDData(BaseModel):
    phred: float | None = None


class GnomADFreq(BaseModel):
    af: float | None = None


class GnomADData(BaseModel):
    af: GnomADFreq | None = None


class ConditionIdentifiers(BaseModel):
    medgen: str | None = None
    mondo: str | None = None
    omim: str | None = None
    orphanet: str | None = None


class ClinVarCondition(BaseModel):
    identifiers: ConditionIdentifiers | None = None
    name: str | None = None
    synonyms: list[str] | None = None

    @field_validator("*", mode="before")
    @classmethod
    def handle_missing(cls, v):
        if v == "":
            return None
        return v


class ClinVarRCV(BaseModel):
    accession: str | None = None
    clinical_significance: str | None = None
    conditions: ClinVarCondition | list[ClinVarCondition] | None = None
    review_status: str | None = None
    last_evaluated: str | None = None
    preferred_name: str | None = None

    @field_validator("conditions", mode="before")
    @classmethod
    def normalize_conditions(cls, v):
        if v is None or v == "":
            return None
        # Handle dict case (single condition)
        if isinstance(v, dict):
            return ClinVarCondition(**v)
        # Handle list case (multiple conditions)
        if isinstance(v, list):
            return [ClinVarCondition(**item) if isinstance(item, dict) else item for item in v]
        return v


class ClinVarData(BaseModel):
    allele_id: int | None = None
    gene: dict[str, Any] | None = None
    rcv: list[ClinVarRCV] | ClinVarRCV | None = None

    @field_validator("rcv", mode="before")
    @classmethod
    def normalize_rcv(cls, v):
        if v is None or v == "":
            return None
        # Handle dict case (single RCV)
        if isinstance(v, dict):
            return ClinVarRCV(**v)
        # Handle list case (multiple RCVs)
        if isinstance(v, list):
            return [ClinVarRCV(**item) if isinstance(item, dict) else item for item in v]
        return v

    def get_all_conditions(self) -> list[str]:
        """Extract all conditions from RCV records"""
        conditions: list[str] = []
        if not self.rcv:
            return conditions

        rcv_list = self.rcv if isinstance(self.rcv, list) else [self.rcv]

        for rcv in rcv_list:
            if not isinstance(rcv, ClinVarRCV) or not rcv.conditions:
                continue

            # After validation, conditions is either ClinVarCondition or List[ClinVarCondition]
            if isinstance(rcv.conditions, ClinVarCondition):
                if rcv.conditions.name:
                    conditions.append(rcv.conditions.name)
            elif isinstance(rcv.conditions, list):
                # All items in list should be ClinVarCondition after validation
                conditions.extend(
                    [cond.name for cond in rcv.conditions if isinstance(cond, ClinVarCondition) and cond.name]
                )

        return conditions

    def get_all_significances(self) -> list[str]:
        """Extract all clinical significances"""
        significances: list[str] = []
        if self.rcv:
            rcv_list = self.rcv if isinstance(self.rcv, list) else [self.rcv]
            significances.extend([rcv.clinical_significance for rcv in rcv_list if rcv.clinical_significance])
        return list(set(significances))  # Unique significances


class MyVariantHit(BaseModel):
    """Direct mapping of MyVariant.info API response"""

    query: str
    cadd: CADDData | None = None
    gnomad_genome: GnomADData | None = None
    clinvar: ClinVarData | None = None

    model_config = ConfigDict(extra="ignore")  # Pydantic v2 config


class MyVariantResult(BaseModel):
    rsid: str
    cadd_phred: float | None = None
    gnomad_af: float | None = None
    clinvar: ClinVarData | None = None
    raw_hit: dict[str, Any] | None = None  # Cache complete API response

    def get_all_conditions(self) -> list[str]:
        return self.clinvar.get_all_conditions() if self.clinvar else []

    def get_all_significances(self) -> list[str]:
        return self.clinvar.get_all_significances() if self.clinvar else []


class PathogenicVariant(BaseModel):
    rsid: str
    genotype: str
    significance: str
    condition: str | None = None


class DrugResponseVariant(BaseModel):
    rsid: str
    genotype: str
    drugs: list[str] = Field(default_factory=list)


class RareVariant(BaseModel):
    rsid: str
    genotype: str
    frequency: float


class HighImpactVariant(BaseModel):
    rsid: str
    genotype: str
    cadd_phred: float
    consequence: str = "High impact"


class AnalysisFindings(BaseModel):
    pathogenic: list[PathogenicVariant] = Field(default_factory=list)
    drug_response: list[DrugResponseVariant] = Field(default_factory=list)
    rare_variants: list[RareVariant] = Field(default_factory=list)
    high_impact: list[HighImpactVariant] = Field(default_factory=list)


class GenomicAnalyzer:
    def __init__(self, filepath: str | Path, output_dir: str | Path | None = None, cache_dir: str | Path | None = None):
        self.filepath = Path(filepath)
        self.output_dir = Path(output_dir) if output_dir else self.filepath.parent
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.df: pd.DataFrame | None = None
        self.findings: defaultdict[str, list[Any]] = defaultdict(list)
        self.errors: list[str | dict[str, str]] = []
        self.processed_count = 0
        self.start_time: datetime | None = None

        # Set up cache directory and database
        self.cache_dir = Path(cache_dir) if cache_dir else self.output_dir / ".cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.cache_db = self.cache_dir / "variant_cache.db"
        self.checkpoint_file = self.cache_dir / "analysis_checkpoint.json"
        self.progress_html = self.output_dir / "genome_analysis_progress.html"
        self.init_cache_db()

    def init_cache_db(self):
        """Initialize SQLite cache database"""
        self.conn = sqlite3.connect(str(self.cache_db))
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS variant_cache (
                rsid TEXT PRIMARY KEY,
                source TEXT,
                data TEXT,
                timestamp DATETIME,
                UNIQUE(rsid, source)
            )
        """)
        self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_timestamp ON variant_cache(timestamp)
        """)
        self.conn.commit()
        print(f"✅ Cache database initialized at {self.cache_db}")

    def get_cached(self, rsid, source, max_age_days=30):
        """Get cached data if it exists and is fresh"""
        cursor = self.conn.execute(
            """
            SELECT data, timestamp FROM variant_cache
            WHERE rsid = ? AND source = ?
        """,
            (rsid, source),
        )

        result = cursor.fetchone()
        if result:
            data, timestamp = result
            cache_time = datetime.fromisoformat(timestamp)
            if datetime.now() - cache_time < timedelta(days=max_age_days):
                return json.loads(data)
        return None

    def set_cached(self, rsid, source, data):
        """Store data in cache"""
        self.conn.execute(
            """
            INSERT OR REPLACE INTO variant_cache (rsid, source, data, timestamp)
            VALUES (?, ?, ?, ?)
        """,
            (rsid, source, json.dumps(data), datetime.now().isoformat()),
        )
        self.conn.commit()

    def load_data(self) -> pd.DataFrame:
        print("📂 Loading DNA data...")
        self.df = pd.read_csv(self.filepath, skiprows=12, low_memory=False)
        self.df["CHROMOSOME"] = self.df["CHROMOSOME"].astype(str)
        print(f"✅ Loaded {len(self.df):,} variants\n")
        return self.df

    def batch_query_myvariant(self, rsids: list[str], batch_size: int = 500) -> dict[str, MyVariantResult]:
        """Batch query MyVariant.info for multiple variants"""
        print(f"🔄 Batch querying MyVariant.info for {len(rsids)} variants...")

        results = {}
        uncached_rsids = []

        # Check cache first
        for rsid in rsids:
            cached = self.get_cached(rsid, "myvariant")
            if cached:
                # Reconstruct from cached dict
                results[rsid] = MyVariantResult(**cached)
            else:
                uncached_rsids.append(rsid)

        print(f"  Found {len(results)} cached, querying {len(uncached_rsids)} new variants")

        # Batch query uncached variants
        for i in range(0, len(uncached_rsids), batch_size):
            batch = uncached_rsids[i : i + batch_size]

            try:
                # MyVariant.info batch endpoint - correct format
                url = "https://myvariant.info/v1/variant"

                # POST endpoint expects JSON body
                headers = {"content-type": "application/json"}
                body = {"ids": batch, "fields": "dbsnp.rsid,cadd.phred,clinvar,gnomad_genome.af.af,pharmgkb"}

                # Add retry logic for network issues
                response = None
                for attempt in range(3):
                    try:
                        response = requests.post(url, json=body, headers=headers, timeout=120)
                        break
                    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
                        if attempt < 2:
                            wait_time = (2**attempt) * 5  # 5, 10, 20 seconds
                            print(f"    Network error, retrying in {wait_time}s...")
                            time.sleep(wait_time)
                        else:
                            raise

                if response is None:
                    raise RuntimeError("MyVariant request returned no response")

                if response.status_code == 200:
                    # Parse response with Pydantic
                    data = response.json()
                    hits = [MyVariantHit(**item) for item in data]

                    for hit in hits:
                        result = MyVariantResult(
                            rsid=hit.query,
                            cadd_phred=hit.cadd.phred if hit.cadd else None,
                            gnomad_af=hit.gnomad_genome.af.af if hit.gnomad_genome and hit.gnomad_genome.af else None,
                            clinvar=hit.clinvar,
                            raw_hit=hit.model_dump(),  # Store complete data
                        )
                        results[hit.query] = result
                        self.set_cached(hit.query, "myvariant", result.model_dump())

                    print(f"  Batch {i // batch_size + 1}: Retrieved {len(hits)} variants")
                else:
                    error = f"MyVariant batch query failed: {response.status_code}"
                    print(f"  ❌ {error}")
                    self.errors.append(error)

            except Exception as e:
                print(f"  ❌ MyVariant batch error: {e}")
                print(f"  Batch: {batch[:3]}...")
                print("  Full traceback:")
                traceback.print_exc()
                raise  # Don't swallow - let it fail

            # Adaptive rate limiting
            if i + batch_size < len(uncached_rsids):
                time.sleep(0.2)

        return results

    def batch_query_clinvar(self, rsids: list[str], batch_size: int = 200) -> dict[str, ClinVarData]:
        """Batch query ClinVar for clinical significance"""
        print(f"🔄 Batch querying ClinVar for {len(rsids)} variants...")

        results = {}
        uncached_rsids = []

        # Check cache
        for rsid in rsids:
            cached = self.get_cached(rsid, "clinvar")
            if cached:
                results[rsid] = cached
            else:
                uncached_rsids.append(rsid)

        if not uncached_rsids:
            print(f"  All {len(results)} variants cached")
            return results

        # Batch query via E-utilities
        for i in range(0, len(uncached_rsids), batch_size):
            batch = uncached_rsids[i : i + batch_size]

            try:
                # Search ClinVar
                base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

                # Build query
                query = " OR ".join([f"{rsid}[Variant ID]" for rsid in batch])

                search_params: dict[str, str | int] = {
                    "db": "clinvar",
                    "term": query,
                    "retmax": batch_size,
                    "retmode": "json",
                }

                search_resp = requests.get(f"{base}/esearch.fcgi", params=search_params, timeout=30)

                if search_resp.status_code == 200:
                    search_data = search_resp.json()
                    id_list = search_data.get("esearchresult", {}).get("idlist", [])

                    if id_list:
                        # Fetch summaries
                        summary_params = {"db": "clinvar", "id": ",".join(id_list), "retmode": "json"}

                        summary_resp = requests.get(f"{base}/esummary.fcgi", params=summary_params, timeout=30)

                        if summary_resp.status_code == 200:
                            summary_data = summary_resp.json()

                            # Process results
                            for uid, record in summary_data.get("result", {}).items():
                                if uid != "uids" and "title" in record:
                                    # Extract rsID from title
                                    title = record["title"]
                                    if "rs" in title:
                                        rsid_match = title.split("rs")[1].split(" ")[0].split(":")[0]
                                        rsid = f"rs{rsid_match}"

                                        if rsid in batch:
                                            results[rsid] = record
                                            self.set_cached(rsid, "clinvar", record)

                            print(f"  Batch {i // batch_size + 1}: Found {len(id_list)} ClinVar entries")

            except Exception as e:
                error = f"ClinVar batch error: {e!s}"
                print(f"  ❌ {error}")
                self.errors.append({"error": error, "traceback": traceback.format_exc()})

            time.sleep(0.3)  # NCBI rate limit

        return results

    def batch_query_ensembl(self, rsids: list[str], batch_size: int = 200) -> dict[str, dict]:
        """Batch query Ensembl VEP"""
        print(f"🔄 Batch querying Ensembl VEP for {len(rsids)} variants...")

        results = {}
        uncached_rsids = []

        # Check cache
        for rsid in rsids:
            cached = self.get_cached(rsid, "ensembl")
            if cached:
                results[rsid] = cached
            else:
                uncached_rsids.append(rsid)

        if not uncached_rsids:
            print(f"  All {len(results)} variants cached")
            return results

        # Batch query
        for i in range(0, len(uncached_rsids), batch_size):
            batch = uncached_rsids[i : i + batch_size]

            try:
                url = "https://rest.ensembl.org/vep/human/id"
                headers = {"Content-Type": "application/json", "Accept": "application/json"}

                # Format for VEP
                data = {"ids": batch}

                response = requests.post(url, headers=headers, json=data, timeout=30)

                if response.status_code == 200:
                    vep_results = response.json()

                    for result in vep_results:
                        if "id" in result:
                            rsid = result["id"]
                            results[rsid] = result
                            self.set_cached(rsid, "ensembl", result)

                    print(f"  Batch {i // batch_size + 1}: Processed {len(vep_results)} variants")
                else:
                    error = f"Ensembl VEP error: {response.status_code}"
                    print(f"  ❌ {error}")
                    self.errors.append(error)

            except Exception as e:
                error = f"Ensembl batch error: {e!s}"
                print(f"  ❌ {error}")
                self.errors.append({"error": error, "traceback": traceback.format_exc()})

            time.sleep(0.5)

        return results

    def save_checkpoint(self, batch_index: int, findings: AnalysisFindings):
        """Save progress checkpoint"""
        checkpoint = {
            "batch_index": batch_index,
            "processed_count": self.processed_count,
            "findings": findings.model_dump(),
            "timestamp": datetime.now().isoformat(),
        }
        with self.checkpoint_file.open("w") as f:
            json.dump(checkpoint, f, indent=2)

    def load_checkpoint(self) -> tuple[int, AnalysisFindings] | None:
        """Load checkpoint if exists"""
        if self.checkpoint_file.exists():
            with self.checkpoint_file.open() as f:
                checkpoint = json.load(f)
            findings = AnalysisFindings(**checkpoint["findings"])
            return checkpoint["batch_index"], findings
        return None

    def update_progress_html(self, findings: AnalysisFindings, total_variants: int, batch_num: int = 0):
        """Update HTML file with current progress and ALL findings"""
        elapsed = (datetime.now() - self.start_time).total_seconds() if self.start_time else 0
        rate = self.processed_count / elapsed if elapsed > 0 else 0
        eta = (total_variants - self.processed_count) / rate if rate > 0 else 0

        # Load template from file
        template_path = Path(__file__).parent / "report_template.html.j2"
        with template_path.open() as f:
            template = Template(f.read())

        # Calculate total batches
        batch_size = 2000
        total_batches = (total_variants + batch_size - 1) // batch_size

        # Sort all findings for display - show most interesting first
        pathogenic_sorted = sorted(
            findings.pathogenic,
            key=lambda x: (
                "pathogenic" in x.significance.lower() and "likely" not in x.significance.lower(),
                x.significance,
            ),
            reverse=True,
        )[:100]  # Show top 100

        # Ultra rare first, then very rare
        rare_sorted = sorted(findings.rare_variants, key=lambda x: x.frequency)[:100]

        # Highest CADD scores first
        high_impact_sorted = sorted(findings.high_impact, key=lambda x: x.cadd_phred or 0, reverse=True)[:100]

        # Determine if we're still processing or done
        is_complete = self.processed_count >= total_variants or batch_num >= total_batches

        html = template.render(
            # Single template handles both progress and final
            is_progress=not is_complete,  # Auto-refresh only if still running
            date=datetime.now().strftime("%Y-%m-%d %H:%M"),
            processed=f"{self.processed_count:,}",
            total=f"{total_variants:,}",
            total_variants=f"{total_variants:,}",
            progress_pct=round(100 * self.processed_count / total_variants, 1) if total_variants > 0 else 0,
            rate=f"{rate:.0f}",
            elapsed=f"{int(elapsed // 60)}m {int(elapsed % 60)}s",
            eta=f"{int(eta // 60)}m {int(eta % 60)}s" if eta < 3600 else f"{eta / 3600:.1f}h",
            pathogenic_count=len(findings.pathogenic),
            rare_count=len(findings.rare_variants),
            high_impact_count=len(findings.high_impact),
            drug_count=len(findings.drug_response),
            batch_num=batch_num,
            total_batches=total_batches,
            start_time=self.start_time.strftime("%Y-%m-%d %H:%M:%S") if self.start_time else "Not started",
            # ALWAYS show the actual variants found
            pathogenic_variants=pathogenic_sorted,
            rare_variants=rare_sorted,
            high_impact_variants=high_impact_sorted,
        )

        with self.progress_html.open("w") as f:
            f.write(html)

    def track_interesting_finding(
        self, rsid: str, genotype: str, data: MyVariantResult, findings: AnalysisFindings
    ) -> str | None:
        """Track and categorize interesting findings, return description if interesting"""
        interesting = None

        # Check clinical significance
        sigs = data.get_all_significances()
        for sig in sigs:
            if "pathogenic" in sig.lower() and "benign" not in sig.lower():
                conditions = data.get_all_conditions()
                findings.pathogenic.append(
                    PathogenicVariant(
                        rsid=rsid, genotype=genotype, significance=sig, condition=conditions[0] if conditions else None
                    )
                )
                interesting = f"⚠️ PATHOGENIC: {rsid} - {sig}"
                break

        # Check rarity
        if data.gnomad_af is not None:
            if data.gnomad_af < 0.00001:  # Ultra rare <0.001%
                findings.rare_variants.append(RareVariant(rsid=rsid, genotype=genotype, frequency=data.gnomad_af))
                if not interesting:
                    interesting = f"💎 ULTRA RARE: {rsid} - {data.gnomad_af:.6%} frequency"
            elif data.gnomad_af < 0.001:  # Very rare <0.1%
                findings.rare_variants.append(RareVariant(rsid=rsid, genotype=genotype, frequency=data.gnomad_af))

        # Check CADD score
        if data.cadd_phred:
            if data.cadd_phred > 30:
                findings.high_impact.append(HighImpactVariant(rsid=rsid, genotype=genotype, cadd_phred=data.cadd_phred))
                if not interesting:
                    interesting = f"🔴 HIGH IMPACT: {rsid} - CADD {data.cadd_phred:.1f}"
            elif data.cadd_phred > 20:
                findings.high_impact.append(HighImpactVariant(rsid=rsid, genotype=genotype, cadd_phred=data.cadd_phred))

        return interesting

    def analyze_all_variants_progressively(self) -> AnalysisFindings:
        """Analyze all variants with progressive output"""
        print("\n🔍 PROGRESSIVE FULL GENOME ANALYSIS")
        print("=" * 60)

        start_time = datetime.now()
        self.start_time = start_time

        # Index dataframe by RSID for fast lookups
        print("Indexing variants for fast lookup...")
        if self.df is None:
            raise RuntimeError("load_data() must be called before analysis")
        df = self.df.set_index("RSID")
        self.df = df

        # Get valid rsIDs
        valid_rsids = [idx for idx in df.index if idx and idx.startswith("rs")]
        total_variants = len(valid_rsids)
        print(f"Found {total_variants:,} valid rsIDs to analyze")

        # Process in optimal batches (MyVariant.info supports up to 1000 per request)
        batch_size = 2000  # Balance between speed and reliability

        # Check for checkpoint
        checkpoint_data = self.load_checkpoint()
        if checkpoint_data:
            start_batch, findings = checkpoint_data
            print(f"📥 Resuming from batch {start_batch}")
            self.processed_count = start_batch * batch_size
        else:
            start_batch = 0
            findings = AnalysisFindings()
            self.processed_count = 0

        # Calculate total batches
        total_batches = (total_variants + batch_size - 1) // batch_size

        print(f"Processing in {total_batches} batches of {batch_size} variants each")
        print(f"Progress will be shown at: {self.progress_html}")
        print("\n" + "-" * 60)

        for batch_idx in range(start_batch, total_batches):
            batch_start = batch_idx * batch_size
            batch_end = min((batch_idx + 1) * batch_size, total_variants)
            batch_rsids = valid_rsids[batch_start:batch_end]

            print(f"\n📦 Batch {batch_idx + 1}/{total_batches} ({len(batch_rsids)} variants)")

            # Query this batch
            try:
                myvariant_results = self.batch_query_myvariant(batch_rsids, batch_size=500)

                # Analyze batch results
                batch_interesting = []

                for rsid in batch_rsids:
                    if rsid not in myvariant_results:
                        continue

                    # Get genotype from indexed dataframe (O(1) lookup)
                    try:
                        genotype = df.loc[rsid, "RESULT"]
                    except KeyError:
                        continue

                    mv_data = myvariant_results[rsid]

                    # Track interesting findings
                    interesting_desc = self.track_interesting_finding(rsid, genotype, mv_data, findings)
                    if interesting_desc:
                        batch_interesting.append(interesting_desc)

                # Update processed count
                self.processed_count = batch_end

                # Print batch summary with interesting findings
                if batch_interesting:
                    print(f"  ✓ Found {len(batch_interesting)} interesting:")
                    for desc in batch_interesting[:3]:  # Show first 3
                        print(f"    {desc}")
                else:
                    print("  ✓ Processed")

                # Save checkpoint
                self.save_checkpoint(batch_idx + 1, findings)

                # Update progress HTML
                self.update_progress_html(findings, total_variants, batch_idx + 1)

                # Print running totals
                print(
                    f"  📊 Totals: {len(findings.pathogenic)} pathogenic | {len(findings.rare_variants)} rare | {len(findings.high_impact)} high-impact"
                )

            except Exception as e:
                print(f"  ❌ Error processing batch {batch_idx + 1}: {e}")
                traceback.print_exc()
                raise  # Don't swallow errors

        print("\n" + "=" * 60)
        print("✅ PROGRESSIVE ANALYSIS COMPLETE!")
        print(f"Analyzed {self.processed_count:,} variants")
        print(f"Time taken: {(datetime.now() - start_time).total_seconds() / 60:.1f} minutes")

        return findings

    def analyze_all_variants_comprehensively(self) -> AnalysisFindings:
        """Legacy comprehensive analysis - use analyze_all_variants_progressively instead"""
        return self.analyze_all_variants_progressively()

    def print_error_summary(self):
        """Print summary of any errors encountered"""
        if self.errors:
            print("\n⚠️  ERRORS ENCOUNTERED:")
            print("=" * 60)
            for error in self.errors[:10]:  # Show first 10 errors
                if isinstance(error, dict):
                    print(f"Error: {error['error']}")
                    if "traceback" in error:
                        print("Traceback (last 3 lines):")
                        tb_lines = error["traceback"].split("\n")
                        for line in tb_lines[-4:-1]:
                            print(f"  {line}")
                else:
                    print(f"  • {error}")

            if len(self.errors) > 10:
                print(f"  ... and {len(self.errors) - 10} more errors")

    def get_cache_stats(self):
        """Get cache statistics"""
        cursor = self.conn.execute("""
            SELECT source, COUNT(*) as count
            FROM variant_cache
            GROUP BY source
        """)

        stats = cursor.fetchall()

        total = self.conn.execute("SELECT COUNT(*) FROM variant_cache").fetchone()[0]

        print("\n📦 CACHE STATISTICS:")
        print(f"  Total cached variants: {total:,}")
        for source, count in stats:
            print(f"  • {source}: {count:,} variants")

        # Cache size
        cache_size = self.cache_db.stat().st_size / (1024 * 1024)
        print(f"  Cache database size: {cache_size:.1f} MB")

    def run_analysis(self):
        """Run optimized analysis with proper error handling and caching"""
        print("\n" + "=" * 70)
        print("     🚀 OPTIMIZED GENOMIC DATABASE ANALYSIS")
        print("=" * 70)
        print("\nFeatures: Batch queries, local caching, comprehensive error handling\n")

        df = self.load_data()

        # Show cache stats
        self.get_cache_stats()

        # Run comprehensive analysis
        findings = self.analyze_all_variants_comprehensively()

        # Save results
        results = {
            "analysis_timestamp": datetime.now().isoformat(),
            "total_variants": len(df),
            "findings": findings.model_dump(),
            "errors": self.errors[:100],  # Limit errors in output
        }

        output_path = self.output_dir / "optimized_analysis_results.json"
        with output_path.open("w") as f:
            json.dump(results, f, indent=2, default=str)

        print(f"\n✅ Results saved to {output_path}")

        # Print error summary
        self.print_error_summary()

        # Final cache stats
        print("\nFinal cache statistics:")
        self.get_cache_stats()

        print("\n" + "=" * 70)
        print("Analysis complete! Check optimized_analysis_results.json for full details")
        print("=" * 70)


def main(argv: list[str] | None = None):
    """Main genome analysis - analyzes all variants progressively"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="MyHeritage raw DNA CSV file to analyze")
    parser.add_argument(
        "--output-dir", type=Path, help="Directory for reports and result files (default: input file directory)"
    )
    parser.add_argument(
        "--cache-dir", type=Path, help="Directory for the SQLite cache and checkpoint (default: output/.cache)"
    )
    args = parser.parse_args(argv)

    print("🧬 FULL GENOME ANALYSIS")
    print("=" * 60)

    analyzer = GenomicAnalyzer(args.input, output_dir=args.output_dir, cache_dir=args.cache_dir)
    analyzer.load_data()

    # Run progressive analysis of ALL variants
    findings = analyzer.analyze_all_variants_progressively()

    # The HTML is already updated throughout, no need for separate final report

    # Print final summary
    print("\n" + "=" * 60)
    print("📊 FINAL SUMMARY:")
    print(f"  ⚠️ Pathogenic variants: {len(findings.pathogenic)}")
    print(f"  💎 Rare variants (<1%): {len(findings.rare_variants)}")
    print(f"  🔴 High impact (CADD>20): {len(findings.high_impact)}")
    print(f"  💊 Drug response markers: {len(findings.drug_response)}")

    # Show top findings
    if findings.pathogenic:
        print("\n⚠️ TOP PATHOGENIC VARIANTS:")
        for pathogenic_var in findings.pathogenic[:5]:
            print(f"  • {pathogenic_var.rsid} ({pathogenic_var.genotype}): {pathogenic_var.significance}")
            if pathogenic_var.condition:
                print(f"    Associated with: {pathogenic_var.condition}")

    if findings.rare_variants:
        print("\n💎 RAREST VARIANTS:")
        for rare_var in sorted(findings.rare_variants, key=lambda x: x.frequency)[:5]:
            print(f"  • {rare_var.rsid} ({rare_var.genotype}): {rare_var.frequency:.5%} frequency")

    print("\n✅ Analysis complete!")
    print("📄 Progress report: genome_analysis_progress.html")
    print("📄 Final report: genome_analysis_report.html")

    return findings


def generate_final_html_report(df, findings: AnalysisFindings, output_path: str | Path = "genome_analysis_report.html"):
    """Generate final comprehensive HTML report using Jinja2"""
    template_path = Path(__file__).parent / "report_template.html.j2"
    with template_path.open() as f:
        template = Template(f.read())

    # Sort variants for display
    pathogenic_sorted = sorted(findings.pathogenic, key=lambda x: x.significance)
    rare_sorted = sorted(findings.rare_variants, key=lambda x: x.frequency)[:100]
    high_impact_sorted = sorted(findings.high_impact, key=lambda x: x.cadd_phred, reverse=True)[:100]

    html = template.render(
        is_progress=False,
        date=datetime.now().strftime("%Y-%m-%d %H:%M"),
        total_variants=f"{len(df):,}",
        pathogenic_count=len(findings.pathogenic),
        rare_count=len(findings.rare_variants),
        high_impact_count=len(findings.high_impact),
        drug_count=len(findings.drug_response),
        pathogenic_variants=pathogenic_sorted,
        rare_variants=rare_sorted,
        high_impact_variants=high_impact_sorted,
    )

    output_path = Path(output_path)
    with output_path.open("w") as f:
        f.write(html)

    print(f"✅ Final report saved to: {output_path}")


def generate_html_report(
    df, results, pathogenic, rare, high_impact, output_path: str | Path = "genome_analysis_report.html"
):
    """Generate comprehensive HTML report"""
    html = f"""<!DOCTYPE html>
<html>
<head>
    <title>Genome Analysis Report - {datetime.now().strftime("%Y-%m-%d")}</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; margin: 40px; background: #f5f5f5; }}
        .container {{ max-width: 1200px; margin: 0 auto; background: white; padding: 30px; border-radius: 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }}
        h1 {{ color: #2c3e50; border-bottom: 3px solid #3498db; padding-bottom: 10px; }}
        h2 {{ color: #34495e; margin-top: 30px; }}
        .summary {{ background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 20px; border-radius: 8px; margin: 20px 0; }}
        .stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 20px; margin: 20px 0; }}
        .stat-card {{ background: #f8f9fa; padding: 20px; border-radius: 8px; text-align: center; }}
        .stat-value {{ font-size: 2em; font-weight: bold; color: #3498db; }}
        .stat-label {{ color: #7f8c8d; margin-top: 5px; }}
        table {{ width: 100%; border-collapse: collapse; margin: 20px 0; }}
        th {{ background: #3498db; color: white; padding: 12px; text-align: left; }}
        td {{ padding: 12px; border-bottom: 1px solid #ecf0f1; }}
        tr:hover {{ background: #f8f9fa; }}
        .pathogenic {{ background: #ffe6e6; }}
        .rare {{ background: #fff3cd; }}
        .high-impact {{ background: #e6f3ff; }}
        .footer {{ text-align: center; color: #7f8c8d; margin-top: 40px; padding-top: 20px; border-top: 1px solid #ecf0f1; }}
    </style>
</head>
<body>
    <div class="container">
        <h1>🧬 Genome Analysis Report</h1>
        <div class="summary">
            <h2 style="color: white; margin-top: 0;">Summary</h2>
            <p>Analysis of {len(df):,} genetic variants from MyHeritage DNA data</p>
            <p>Database: MyVariant.info (ClinVar, gnomAD, CADD)</p>
            <p>Generated: {datetime.now().strftime("%Y-%m-%d %H:%M")}</p>
        </div>

        <div class="stats">
            <div class="stat-card">
                <div class="stat-value">{len(df):,}</div>
                <div class="stat-label">Total Variants</div>
            </div>
            <div class="stat-card">
                <div class="stat-value">{len(results)}</div>
                <div class="stat-label">Variants Analyzed</div>
            </div>
            <div class="stat-card">
                <div class="stat-value">{len(pathogenic)}</div>
                <div class="stat-label">Pathogenic</div>
            </div>
            <div class="stat-card">
                <div class="stat-value">{len(rare)}</div>
                <div class="stat-label">Rare (<1%)</div>
            </div>
        </div>
"""

    if pathogenic:
        html += """
        <h2>⚠️ Pathogenic Variants</h2>
        <table>
            <tr><th>Variant</th><th>Genotype</th><th>Significance</th><th>Associated Conditions</th></tr>
"""
        for rsid, geno, sig, conditions in pathogenic[:20]:
            cond_str = "<br>".join(conditions[:3]) if conditions else "-"
            html += f"""
            <tr class="pathogenic">
                <td><a href="https://www.ncbi.nlm.nih.gov/snp/{rsid}" target="_blank">{rsid}</a></td>
                <td>{geno}</td>
                <td>{sig}</td>
                <td>{cond_str}</td>
            </tr>
"""
        html += "</table>"

    if rare:
        html += """
        <h2>💎 Rare Variants</h2>
        <table>
            <tr><th>Variant</th><th>Genotype</th><th>Population Frequency</th></tr>
"""
        for rsid, geno, freq in sorted(rare, key=lambda x: x[2])[:20]:
            html += f"""
            <tr class="rare">
                <td><a href="https://www.ncbi.nlm.nih.gov/snp/{rsid}" target="_blank">{rsid}</a></td>
                <td>{geno}</td>
                <td>{freq:.4%}</td>
            </tr>
"""
        html += "</table>"

    if high_impact:
        html += """
        <h2>🔴 High Impact Variants (CADD >25)</h2>
        <table>
            <tr><th>Variant</th><th>Genotype</th><th>CADD Score</th></tr>
"""
        for rsid, geno, cadd in sorted(high_impact, key=lambda x: x[2], reverse=True)[:20]:
            html += f"""
            <tr class="high-impact">
                <td><a href="https://www.ncbi.nlm.nih.gov/snp/{rsid}" target="_blank">{rsid}</a></td>
                <td>{geno}</td>
                <td>{cadd:.1f}</td>
            </tr>
"""
        html += "</table>"

    html += """
        <div class="footer">
            <p>This report is for research purposes only. Consult a healthcare professional for medical advice.</p>
        </div>
    </div>
</body>
</html>
"""

    with Path(output_path).open("w") as f:
        f.write(html)


if __name__ == "__main__":
    try:
        results = main()
    except Exception as e:
        print(f"❌ Analysis failed: {e}")
        import traceback

        traceback.print_exc()
