"""Salary/bond/seniority/freshness text to numbers + eligibility gate. INR-centric heuristics.

ponytail: single-currency parser (INR). Add a currency map when non-India boards land.
"""
import re
from datetime import date, datetime, timedelta

import config

_NUM = re.compile(r"\d+(?:[.,]\d{2,3})*(?:\.\d+)?")
_MONTHLY = ("month", "/mo", "p.m", "per mo", "monthly", "stipend/mo")
_SAL_CUE = ("₹", "rs", "inr", "lpa", "lakh", "lac", "crore", "ctc", "salary", "stipend",
            "annum", "annual", "/yr", "per year", "per annum") + _MONTHLY
_NO_BOND = ("no bond", "none", "nil", "not required", "no service agreement", "0 year", "n/a")
_BOND_UNIT = re.compile(r"(\d+(?:\.\d+)?)\s*(year|yr|month|mo)")

# A title carrying one of these is a req a zero-experience candidate cannot win, whatever the body
# claims. "associate" is deliberately absent — it is an entry grade at most Indian service firms.
_SENIOR_TITLE = re.compile(
    r"\b(?:senior|sr\.?|snr|lead|principal|staff|architect|manager|head|chief|director|vp|"
    r"ii|iii|iv|l[2-9]|t[2-9]|tier[\s-]?[2-9]|expert|specialist\s+lead)\b", re.I)
_ENTRY_TITLE = re.compile(
    r"\b(?:fresher|freshers|entry[\s-]?level|graduate|trainee|associate|apprentice|"
    r"intern|internship|junior|jr\.?|l1|tier[\s-]?1)\b", re.I)
# Year figures only count when the sentence is talking about experience: "a 25 year old company"
# and "10 years in the market" are marketing copy, not a requirement.
_EXP_SENTENCE = re.compile(r"[^.\n;|]{0,120}\b(?:experience|exp\.?|exposure)\b[^.\n;|]{0,120}", re.I)
_EXP_RANGE = re.compile(r"(\d{1,2})\s*(?:-|–|—|to)\s*(\d{1,2})\s*\+?\s*(?:years?|yrs?)", re.I)
_EXP_PLUS = re.compile(r"(\d{1,2})\s*\+\s*(?:years?|yrs?)", re.I)
_EXP_MIN = re.compile(
    r"(?:minimum|min\.?|at\s?least|atleast|over|more\s+than)\s*(?:of\s*)?(\d{1,2})\s*(?:years?|yrs?)", re.I)

# Core domain: security, cloud, DevOps, SRE. Boards answer "Junior Cloud Engineer" with
# "Data Scientist", and an off-domain application is a wasted slot plus a bad signal to the recruiter.
_TOPIC = re.compile(
    r"\b(?:security|secure|cyber\w*|infosec|appsec|devsecops|soc|siem|soar|edr|xdr|dlp|"
    r"vulnerability|penetration|pentest|pen[\s-]?test\w*|exploit|threat|malware|phish\w*|"
    r"forensic\w*|incident\s+response|iam|identity\s+and\s+access|okta|entra|active\s+directory|"
    r"cloud|aws|azure|gcp|google\s+cloud|kubernetes|k8s|docker|container\w*|terraform|ansible|"
    r"devops|sre|site\s+reliability|platform\s+engineer\w*|ci\s?/?\s?cd|jenkins|gitlab|argocd|"
    r"release\s+engineer\w*|release\s+management|build\s+(?:and\s+release\s+)?engineer\w*|"
    r"automation\s+engineer\w*|ethical\s+hack\w*|hacker|red\s+team|blue\s+team|purple\s+team|"
    r"observability|prometheus|grafana|linux|unix|infrastructure|network\w*|firewall|vpn|"
    r"iso\s?27001|soc\s?2|nist|owasp|compliance|audit\w*|hardening|encryption|pki)\b", re.I)
# Title says where this req sits among the four role families (config.ROLES). Longest-first: a "Full
# Stack Developer" is fullstack before it is software, and a "Cloud Security Engineer" is security
# before it is cloud. Checked title-first so a software title stays software however much AWS its JD
# mentions, with the description as the fallback for a title that names no family at all.
_ROLE_WORDS = {
    "fullstack": re.compile(
        r"\b(?:full[\s-]?stack|mern|mean\s+stack|web\s+(?:developer|application\w*|design\w*)|"
        r"website\s+developer|front[\s-]?end|react|angular|vue(?:\.?js)?|express(?:\.?js)?|"
        r"node\.?\s?js)\b", re.I),
    "security": re.compile(
        r"\b(?:security|secure|cyber\w*|infosec|appsec|soc|siem|soar|edr|xdr|dlp|"
        r"vulnerability|penetration|pentest|pen[\s-]?test\w*|exploit|threat|malware|phish\w*|"
        r"forensic\w*|incident|iam|identity\s+and\s+access|okta|entra|active\s+directory|"
        r"owasp|hack\w*|red\s+team|blue\s+team|purple\s+team|hardening|encryption|pki)\b", re.I),
    "devops": re.compile(
        r"\b(?:devops|devsecops|sre|site\s+reliability|cloud|aws|azure|gcp|google\s+cloud|"
        r"kubernetes|k8s|docker|container\w*|terraform|ansible|jenkins|gitlab|argocd|ci\s?/?\s?cd|"
        r"platform\s+engineer\w*|release\s+engineer\w*|build\s+(?:and\s+release\s+)?engineer\w*|"
        r"infrastructure|observability|prometheus|grafana|automation\s+engineer\w*|linux|unix|"
        r"network\w*|firewall|vpn)\b", re.I),
    "software": re.compile(
        r"\b(?:software|sde|programmer|developer|application\s+development|java|python|c\+\+|"
        r"c#|\.net|dotnet|backend|back[\s-]?end)\b", re.I),
}
_ROLE_ORDER = ("fullstack", "security", "devops", "software")


def role_of(job) -> str:
    """Which family (config.ROLES) this req belongs to, or '' when nothing names one.

    Drives the daily mixture and the dashboard's role filter, so it has to be stable for a row read
    back from the DB - the value is stored once, at scrape time, in the one place every board's rows
    pass through.
    """
    for field in ("title", "description"):
        text = str(job.get(field) or "")
        if not text:
            continue
        for role in _ROLE_ORDER:
            if _ROLE_WORDS[role].search(text):
                return role
    return ""


# The software and web families, searched for alongside the domain above. Kept as its own pattern
# because _TOPIC's words are tools and this one's are job titles: bare "react" or "python" in a JD
# means the tool is mentioned, not that the req is a developer req, and the description path already
# demands two distinct hits before it will carry a neutral title.
_DEV_TOPIC = re.compile(
    r"\b(?:software\s+(?:developer|engineer|trainee|programmer|analyst|development)|sde|programmer|"
    r"java\s+developer|python\s+developer|c\+\+\s+developer|c#\s+developer|dotnet\s+developer|"
    r"application\s+developer|full[\s-]?stack|mern|mean\s+stack|web\s+(?:developer|application\w*)|"
    r"website\s+developer|front[\s-]?end\s+developer|back[\s-]?end\s+developer|"
    r"react(?:\s?\.?js)?\s+developer|node\.?\s?js\s+developer|angular\s+developer)\b"
    r"|\.net\s+developer\b", re.I)
# Titles that are unambiguously another discipline. Read AFTER the domain word that wins, and BEFORE
# the developer families, so a vendor's "SFCC Backend Developer" or a "React Native Developer" is
# still another discipline while a plain "Backend Developer" is one of ours.
_OFF_TOPIC = re.compile(
    r"\b(?:data\s+scien\w*|data\s+analyst|data\s+engineer|business\s+analyst|react\s+native|"
    r"android|ios\s+developer|flutter|php|laravel|ui/?ux|wordpress|"
    r"salesforce|sfcc|sap|workday|oracle\s+apps|power\s?bi|tableau|machine\s+learning|ml\s+engineer|"
    r"etl|quality\s+assurance|manual\s+testing|test\s+engineer|sdet|"
    r"sales|marketing|bpo|telecaller|recruiter|accountant|nurse|teacher|"
    r"content\s+writer|graphic\s+design\w*|video\s+edit\w*)\b", re.I)

# Tech giants and mass-hiring service firms. They post the same req across every city, leave it open
# for a week and screen thousands, so the freshness/competition heuristics below are relaxed for them.
ENTERPRISE = (
    "tcs", "tata consultancy", "wipro", "infosys", "cognizant", "accenture", "capgemini", "hcl",
    "hcltech", "techmahindra", "tech mahindra", "ltimindtree", "mindtree", "mphasis", "birlasoft",
    "coforge", "hexaware", "virtusa", "zensar", "persistent systems", "cybage", "sonata software",
    "genpact", "concentrix", "wns", "atos", "dxc", "kyndryl", "ntt data", "syntel", "igate",
    "deloitte", "accenture solutions", "pwc", "kpmg", "ernst", "grant thornton",
    "ibm", "google", "amazon", "amazon web services", "microsoft", "meta", "apple", "oracle", "sap",
    "cisco", "dell", "emc", "intel", "nvidia", "adobe", "salesforce", "vmware", "broadcom",
    "qualcomm", "juniper", "arista", "netapp", "hpe", "hewlett", "siemens", "bosch", "honeywell",
    "paypal", "visa", "mastercard", "goldman", "morgan", "barclays", "citi", "hsbc", "jpmorgan",
    "walmart", "flipkart", "swiggy", "zomato", "paytm", "phonepe", "razorpay", "zoho", "freshworks",
)
# Single words match whole tokens, never substrings: "contains" would turn Sapient into SAP,
# Metacube into Meta and Honeywell into a false EY hit. Phrases match as substrings so a legal
# suffix cannot break them apart.
_ENTERPRISE_WORDS = frozenset(g for g in ENTERPRISE if " " not in g)
_ENTERPRISE_PHRASES = tuple(g for g in ENTERPRISE if " " in g)

# Boards report posting age at day granularity, in words ("5 days ago") or as a bare date.
_AGO = re.compile(r"(\d{1,3})\s*\+?\s*(minute|min|hour|hr|day|week|month|year)s?\s*(?:ago|old)", re.I)
_AGO_DAYS = {"minute": 0, "min": 0, "hour": 0, "hr": 0, "day": 1, "week": 7, "month": 30, "year": 365}
_APPLICANTS = re.compile(
    r"(?:over\s+|more\s+than\s+)?(\d{1,6})\s*\+?\s*"
    r"(?:applicants|applications\s+received|candidates\s+applied|people\s+clicked\s+apply)", re.I)


def _nums(text: str) -> list[float]:
    out = []
    for m in _NUM.finditer(text):
        try:
            out.append(float(m.group().replace(",", "")))
        except ValueError:
            pass
    return out


def _cued_nums(text: str) -> list[float]:
    """Numbers that sit next to a salary cue or are grouped rupee figures - the ones that are pay."""
    out = []
    for m in _NUM.finditer(text):
        raw = m.group()
        window = text[max(0, m.start() - 8):m.end() + 8]
        grouped = "," in raw or len(raw.replace(".", "")) >= 6
        if grouped or any(c in window for c in _SAL_CUE):
            try:
                out.append(float(raw.replace(",", "")))
            except ValueError:
                pass
    return out


def lpa(text) -> float | None:
    """Any salary string to lakhs-per-annum. None when no number present.

    A stray number ('team of 50') must not read as pay, so cued numbers win; a range keeps its
    upper bound, since both ends are cued and max() picks the larger.
    """
    if not text:
        return None
    t = str(text).lower()
    nums = _nums(t)
    if not nums:
        return None
    cued = _cued_nums(t)
    value = max(cued) if cued else max(nums)
    if any(k in t for k in _MONTHLY):
        value *= 12
    return round(value / 100_000, 2) if value >= 1000 else round(value, 2)


def bond_years(text) -> float:
    """Bond/service-agreement length in years. Unknown or absent to 0."""
    if not text:
        return 0.0
    t = str(text).lower()
    if any(k in t for k in _NO_BOND):
        return 0.0
    m = _BOND_UNIT.search(t)
    if m:
        n = float(m.group(1))
        return round(n / 12, 2) if m.group(2).startswith("mo") else n
    nums = [n for n in _nums(t) if n <= 10]
    return nums[0] if nums else 0.0


def senior_marker(title) -> str | None:
    """The seniority word in a job title, if any. Cheapest reliable reject signal there is.

    camelCase is split first, or a glued title ("SeniorAdministrator") escapes the word boundary.
    """
    m = _SENIOR_TITLE.search(re.sub(r"(?<=[a-z])(?=[A-Z])", " ", str(title or "")))
    return m.group(0) if m else None


def experience_floor(text) -> float | None:
    """Lowest number of years the listing actually demands. None when it never says.

    Ranges contribute their lower bound ("3-5 years" to 3). When a JD states several floors the
    smallest wins: "5+ years overall, 2+ in Linux" is ambiguous, and over-rejecting a real fresher
    opening costs more than one wasted queue slot. Only unambiguous senior asks get filtered.
    """
    if not text:
        return None
    floors: list[float] = []
    for sentence in _EXP_SENTENCE.findall(str(text)):
        floors += [float(lo) for lo, _hi in _EXP_RANGE.findall(sentence)]
        rest = _EXP_RANGE.sub(" ", sentence)          # a range already contributed its lower bound
        for rx in (_EXP_PLUS, _EXP_MIN):
            floors += [float(n) for n in rx.findall(rest)]
    return min(floors) if floors else None


def entry_level(job) -> bool:
    """Advertises itself as fresher-friendly. Ranking hint, not a gate — most JDs stay silent."""
    if _SENIOR_TITLE.search(str(job.get("title") or "")):
        return False
    if _ENTRY_TITLE.search(str(job.get("title") or "")):
        return True
    floor = experience_floor(job.get("description"))
    return floor is not None and floor <= 1


def topic_ok(job) -> tuple[bool, str]:
    """Is this a security/cloud/DevOps req or a developer one? Checked before pay and seniority.

    A domain word in the TITLE is the only strong signal, and either family's word counts as one: a
    "Junior Cloud Engineer" and a "MERN Stack Developer" are both in scope now. Description-only
    matching is weak on its own — nearly every modern JD says "AWS" somewhere — so the description
    path needs two distinct keywords before it can carry a title that names no family at all.
    """
    title = str(job.get("title") or "")
    if m := _TOPIC.search(title):
        return True, f"title says {m.group(0).lower()!r}"
    if m := _OFF_TOPIC.search(title):
        return False, f"off-domain title {m.group(0).lower()!r}"
    if m := _DEV_TOPIC.search(title):
        return True, f"title says {m.group(0).lower()!r}"
    found = {m.group(0).lower() for rx in (_TOPIC, _DEV_TOPIC)
             for m in rx.finditer(str(job.get("description") or ""))}
    if len(found) >= 2:
        return True, f"description says {', '.join(sorted(found)[:3])}"
    return False, f"no domain keyword in title, {len(found)} in description"


def enterprise(company) -> bool:
    """Known giant or mass-hiring service firm — the bypass list for the metadata gates."""
    name = str(company or "").lower()
    if any(phrase in name for phrase in _ENTERPRISE_PHRASES):
        return True
    return bool(_ENTERPRISE_WORDS & set(re.split(r"[^a-z0-9]+", name)))


def posted_date(value) -> date | None:
    """Any board's posted-at field to a calendar date. None when it is missing or unparseable.

    jobspy hands back a date, Internshala hands back "5 days ago". Neither carries a clock time, so
    hour precision does not exist upstream and every age below is measured in whole days x 24.
    """
    if value is None or value == "" or value != value:        # NaN from a pandas column
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if callable(getattr(value, "date", None)):                # pandas Timestamp
        try:
            return value.date()
        except Exception:
            pass
    text = str(value).strip().lower()
    if m := _AGO.search(text):
        return date.today() - timedelta(days=int(m.group(1)) * _AGO_DAYS[m.group(2).lower()])
    if any(k in text for k in ("just now", "just posted", "today", "few hours", "hours ago")):
        return date.today()
    if "yesterday" in text:
        return date.today() - timedelta(days=1)
    try:
        return datetime.fromisoformat(text[:19].replace("z", "").strip()).date()
    except ValueError:
        return None


def posting_age_hours(job) -> float | None:
    """Age of the listing in hours, day-granular. None when the board never said."""
    posted = posted_date(job.get("date_posted"))
    return None if posted is None else max(0.0, (date.today() - posted).days * 24.0)


def applicant_count(job) -> int | None:
    """Applicants on the listing. None means the board did not report one, which is not a reject.

    ponytail: none of the current backends expose this field, so today the value comes only from a
    description that states it in words. Populate job['applicants'] if a scraper ever surfaces it.
    """
    for key in ("applicants", "applicant_count", "applies", "num_applicants"):
        raw = job.get(key)
        if raw is None or raw == "" or raw != raw:
            continue
        try:
            return int(float(raw))
        except (TypeError, ValueError):
            if m := _APPLICANTS.search(str(raw)):
                return int(m.group(1))
    m = _APPLICANTS.search(str(job.get("description") or ""))
    return int(m.group(1)) if m else None


def eligible(job) -> tuple[bool, str]:
    """Gate before any irreversible action. Hidden salary passes — most listings hide it.

    Missing metadata always passes: a board that reports no date or no applicant count must not
    silently zero the day's intake. Only a value that explicitly breaks a limit rejects.
    """
    on_topic, why = topic_ok(job)
    if not on_topic:
        return False, f"off-domain: {why}"
    big = enterprise(job.get("company"))
    fresh_limit = config.FRESH_HOURS_ENTERPRISE if big else config.FRESH_HOURS
    age = posting_age_hours(job)
    if age is not None and age > fresh_limit:
        return False, f"posted {age / 24:g}d ago > {fresh_limit}h limit"
    if not big and (crowd := applicant_count(job)) is not None and crowd > config.MAX_APPLICANTS:
        return False, f"{crowd} applicants > {config.MAX_APPLICANTS}"
    pay, bond = lpa(job.get("salary")), bond_years(job.get("bond_terms"))
    if pay is not None and pay < config.MIN_LPA:
        return False, f"salary {pay:g} LPA < {config.MIN_LPA} LPA"
    if bond > config.MAX_BOND_YEARS:
        return False, f"bond {bond:g}y > {config.MAX_BOND_YEARS}y"
    if marker := senior_marker(job.get("title")):
        return False, f"senior title: {marker.lower()!r}"
    floor = experience_floor(f"{job.get('title') or ''}\n{job.get('description') or ''}")
    if floor is not None and floor > config.MAX_EXPERIENCE_YEARS:
        return False, f"wants {floor:g}y experience > {config.MAX_EXPERIENCE_YEARS}y"
    return True, "ok"


_TRACKS = {
    "cybersecurity": ("security", "cyber", "infosec", "soc", "siem", "soar", "edr", "xdr", "threat",
                      "incident", "vulnerability", "penetration", "pentest", "malware", "forensic",
                      "iam", "appsec", "owasp", "phishing", "red team", "blue team"),
    "cloud": ("cloud", "aws", "azure", "gcp", "google cloud", "kubernetes", "k8s", "docker",
              "terraform", "ansible", "container", "serverless", "ec2", "s3", "lambda"),
    "devops": ("devops", "ci/cd", "cicd", "jenkins", "gitlab", "argocd", "sre", "site reliability",
               "platform engineer", "release engineer", "build engineer", "observability",
               "prometheus", "grafana", "pipeline", "automation"),
    "devsecops": ("devsecops", "shift left", "shift-left", "sast", "dast", "sca", "trivy", "sonarqube",
                  "zap", "supply chain", "secure ci", "security gate", "container hardening"),
    "software": ("software", "sde", "java", "python", "c++", "c#", ".net", "programming", "oops",
                 "data structures", "dbms", "rest api", "sql", "unit test"),
    "fullstack": ("full stack", "fullstack", "mern", "mean stack", "react", "node", "express",
                  "mongodb", "javascript", "html", "css", "web developer", "frontend", "backend"),
}


def fit_score(job) -> tuple[float, str]:
    """JD keyword overlap to (0..1 score, best-matching track). Empty track when nothing matches."""
    text = f"{job.get('title') or ''} {job.get('description') or ''}".lower()
    if not text.strip():
        return 0.0, ""
    hits = {tr: sum(1 for kw in kws if kw in text) for tr, kws in _TRACKS.items()}
    track = max(hits, key=hits.get)
    total = sum(hits.values())
    return round(min(1.0, total / 8), 2), (track if hits[track] else "")


def _selfcheck():
    assert lpa("12 LPA") == 12
    assert lpa("₹8,00,000 - ₹12,00,000 a year") == 12
    assert lpa("1200000") == 12
    assert lpa("40,000 per month") == 4.8
    assert lpa("25000/month stipend") == 3.0
    assert lpa("Not disclosed") is None and lpa(None) is None
    assert lpa("3 LPA, team of 50") == 3.0, "a stray non-salary number must not read as pay"
    assert lpa("₹6,00,000 per year, 200 people") == 6.0, "headcount is not salary"
    assert lpa("3.5 - 6 LPA") == 6.0 and lpa("8-12 Lacs PA") == 12.0, "range keeps its upper bound"
    assert bond_years("No bond") == 0.0 and bond_years(None) == 0.0
    assert bond_years("2 year service agreement") == 2.0
    assert bond_years("18 months bond") == 1.5
    assert bond_years("bond: 4 yrs") == 4.0
    assert bond_years("3 year bond") == 3.0

    assert senior_marker("Lead DevSecOps Engineer") == "Lead"
    assert senior_marker("Sr. Cloud Engineer") and senior_marker("Principal SRE")
    assert senior_marker("Security Engineer III") and senior_marker("Head of Infrastructure")
    assert senior_marker("Junior DevOps Engineer") is None, "junior must survive the senior regex"
    assert senior_marker("SOC Analyst Fresher") is None and senior_marker(None) is None
    assert senior_marker("Cloud Support Associate") is None, "associate is an entry grade here"
    assert senior_marker("CYBER SECURITY ANALYST L3") and senior_marker("SOC Analyst T2")
    assert senior_marker("Security Engineer II - Vulnerability Management")
    assert senior_marker("SeniorAdministrator - Security Analysis, SIEM") == "Senior"
    assert senior_marker("L1 SOC Analyst") is None, "l1 is the entry rung of the same ladder"
    assert senior_marker("Cloud Ops - L2TP Support") is None, "l2 needs a word boundary, not a prefix"

    assert experience_floor("5+ years of experience with Kubernetes") == 5
    assert experience_floor("Experience: 3-5 years") == 3
    assert experience_floor("Minimum 4 years experience required") == 4
    assert experience_floor("0-2 years experience") == 0
    assert experience_floor("5+ years experience overall; 2+ years experience on Linux") == 2
    assert experience_floor("A trusted brand with 25 years in the market.") is None, "not a requirement"
    assert experience_floor("Founded 30 years ago. Experience with Python preferred.") is None
    assert experience_floor(None) is None and experience_floor("no numbers at all") is None

    assert entry_level({"title": "L1 SOC Analyst"}) is True
    assert entry_level({"title": "DevOps Engineer Fresher"}) is True
    assert entry_level({"title": "Cloud Engineer", "description": "0-1 years experience"}) is True
    assert entry_level({"title": "Cloud Engineer", "description": "8+ years experience"}) is False
    assert entry_level({"title": "Junior Architect"}) is False, "senior word beats the junior word"
    assert entry_level({"title": "Cloud Engineer"}) is False

    assert topic_ok({"title": "Junior Cloud Engineer"})[0] is True
    assert topic_ok({"title": "L1 SOC Analyst"})[0] is True
    assert topic_ok({"title": "Associate Infrastructure Engineer (L1)"})[0] is True
    assert topic_ok({"title": "Data Scientist", "description": "AWS, Docker, Kubernetes daily"})[0] is False, \
        "off-domain title must beat a body full of cloud tooling"
    assert topic_ok({"title": "Sr. SFCC Backend Developer"})[0] is False, "another vendor's stack"
    assert topic_ok({"title": "React Native Developer"})[0] is False, "mobile is not a web req"
    assert topic_ok({"title": "Data Analyst", "description": "React and Node.js dashboards"})[0] is False
    assert topic_ok({"title": "Software Developer Fresher"})[0] is True
    assert topic_ok({"title": "MERN Stack Developer"})[0] is True
    assert topic_ok({"title": "Junior Backend Developer"})[0] is True
    assert topic_ok({"title": "Web Developer"})[0] is True
    assert topic_ok({"title": "Trainee Engineer", "description":
                     "Build web applications on the MERN stack"})[0] is True
    assert topic_ok({"title": "Trainee Engineer", "description":
                     "MERN stack exposure helps"})[0] is False, "one keyword is not enough here either"
    assert topic_ok({"title": "Support Engineer", "description": "Triage AWS alerts, patch Linux hosts"})[0] is True
    assert topic_ok({"title": "Support Engineer", "description": "Answer customer tickets"})[0] is False
    assert topic_ok({"title": "Support Engineer", "description": "Some AWS exposure helps"})[0] is False, \
        "one description keyword is not enough on its own"
    assert topic_ok({})[0] is False and topic_ok({"title": None})[0] is False
    off = [t for t in config.SEARCH_TERMS if not topic_ok({"title": t})[0]]
    assert not off, f"search terms the gate would reject: {off}"

    rich = fit_score({"title": "DevSecOps Engineer", "description":
                      "SAST DAST Trivy Kubernetes CI/CD Jenkins AWS security hardening"})
    poor = fit_score({"title": "Office Assistant", "description": "filing and phone calls"})
    assert 0.0 <= poor[0] < rich[0] <= 1.0, (poor, rich)
    assert rich[1] in _TRACKS and poor[1] == "", (rich, poor)
    assert fit_score({})[0] == 0.0

    cloud = {"title": "Junior Cloud Engineer"}
    assert eligible(cloud | {"salary": "3 LPA", "bond_terms": ""})[0] is False
    assert eligible(cloud | {"salary": "9 LPA", "bond_terms": "4 years"})[0] is False
    assert eligible(cloud | {"salary": "9 LPA", "bond_terms": "2 years"})[0] is True
    assert eligible(cloud | {"salary": None, "bond_terms": None})[0] is True
    assert eligible({"title": "Data Scientist", "salary": "20 LPA"})[0] is False, "topic beats pay"
    assert eligible({"title": "Lead Cloud Architect"})[0] is False
    assert eligible({"title": "Junior SRE", "description": "10+ years of experience"})[0] is False
    assert eligible({"title": "Junior SRE", "description": "0-2 years experience"})[0] is True
    assert eligible({"title": "Junior SRE", "description": "1-3 years experience"})[0] is False, \
        "a floor of 1 is no longer a fresher req"
    assert eligible({"title": "Software Developer Fresher", "description":
                     "0-4 years experience"})[0] is True, "a range that starts at 0 is a fresher req"
    assert eligible({"title": "MERN Stack Developer", "description":
                     "2-4 years experience"})[0] is False, "a floor of 2 is out"
    assert eligible({"title": "SOC Analyst Fresher", "salary": "6 LPA",
                     "bond_terms": "3 year service agreement"})[0] is True
    assert eligible({"title": "Cloud Support Associate", "description":
                     "0-2 years experience welcome. Freshers may apply."})[0] is True, "lower bound rules"

    assert role_of({"title": "Software Developer Fresher"}) == "software"
    assert role_of({"title": "MERN Stack Developer"}) == "fullstack"
    assert role_of({"title": "Junior Backend Developer"}) == "software", "backend alone is not web"
    assert role_of({"title": "Cloud Security Associate"}) == "security", "security beats cloud"
    assert role_of({"title": "Junior DevSecOps Engineer"}) == "devops"
    assert role_of({"title": "SOC Analyst Fresher"}) == "security"
    assert role_of({"title": "Junior Cloud Engineer"}) == "devops"
    assert role_of({"title": "Software Engineer", "description": "React, Node.js and MongoDB"}) \
        == "software", "the title owns the family, not the JD's tool list"
    assert role_of({"title": "Technical Support Engineer", "description": "AWS and Linux hosts"}) \
        == "devops", "a title that names no family is read from the description"
    assert role_of({"title": "Office Assistant"}) == ""
    assert all(role_of({"title": t}) in config.ROLES for t in config.SEARCH_TERMS), \
        [t for t in config.SEARCH_TERMS if role_of({"title": t}) not in config.ROLES]
    assert len(config.SEARCH_TERMS) == 4 * len(config.SOFTWARE_TERMS), "the interleave dropped terms"

    assert enterprise("Tata Consultancy Services") and enterprise("TCS")
    assert enterprise("Infosys BPM Ltd") and enterprise("Amazon Development Centre India")
    assert enterprise("Wipro Limited") and enterprise("IBM India Private Limited")
    assert enterprise("Microsoft") and enterprise("Tech Mahindra") and enterprise("HCLTech")
    assert not enterprise("Sapient Consulting"), "'sap' must not match inside a word"
    assert not enterprise("Metacube Software"), "'meta' must not match inside a word"
    assert not enterprise("CitiusTech Healthcare"), "'citi' must not match inside a word"
    assert not enterprise("Acme Cloud Labs") and not enterprise("") and not enterprise(None)

    today, yday = date.today(), date.today() - timedelta(days=1)
    assert posted_date(yday) == yday and posted_date(datetime(2026, 8, 20, 9, 30)) == date(2026, 8, 20)
    assert posted_date("2026-08-20") == date(2026, 8, 20)
    assert posted_date("2026-08-20T09:30:00Z") == date(2026, 8, 20)
    assert posted_date("5 days ago") == today - timedelta(days=5)
    assert posted_date("2 weeks ago") == today - timedelta(days=14)
    assert posted_date("30+ days ago") == today - timedelta(days=30)
    assert posted_date("3 hours ago") == today and posted_date("Just now") == today
    assert posted_date("Yesterday") == yday
    assert posted_date(None) is None and posted_date("") is None and posted_date("whenever") is None
    assert posted_date(float("nan")) is None
    assert posting_age_hours({"date_posted": today}) == 0.0
    assert posting_age_hours({"date_posted": "2 days ago"}) == 48.0
    assert posting_age_hours({}) is None, "no date is not an age of zero"

    startup = {"title": "Junior Cloud Engineer", "company": "Acme Labs"}
    giant = {"title": "Junior Cloud Engineer", "company": "Infosys Ltd"}
    assert eligible(startup | {"date_posted": "1 day ago"})[0] is True
    assert eligible(startup | {"date_posted": "2 days ago"})[0] is False, "24h limit off the bypass list"
    assert eligible(giant | {"date_posted": "2 days ago"})[0] is True, "giants get 48h"
    assert eligible(giant | {"date_posted": "3 days ago"})[0] is False
    assert eligible(startup)[0] is True, "missing date must not reject"

    assert applicant_count({"applicants": 12}) == 12 and applicant_count({"applies": "88"}) == 88
    assert applicant_count({"description": "Over 200 applicants"}) == 200
    assert applicant_count({"description": "100+ applicants so far"}) == 100
    assert applicant_count({"description": "We serve 500 clients"}) is None, "not an applicant count"
    assert applicant_count({}) is None
    assert eligible(startup | {"applicants": 40})[0] is True
    assert eligible(startup | {"applicants": 400})[0] is False
    assert eligible(giant | {"applicants": 4000})[0] is True, "giants ignore the crowd"
    print("parsing.py selfcheck ok")


if __name__ == "__main__":
    _selfcheck()
