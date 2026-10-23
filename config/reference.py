"""Government-fixed reference data for the PHOL feed (data.gov.au Private Health
Insurance dataset, PHOLSchema-V3.1.xsd).

This is NOT business config: nobody at Navy Health edits "what JointReplacements
means" or "which tiers exist" — the schema does, and only when the Australian
government revises it. That is why this lives in code next to the ingest
parser and the engine, not in config/materiality.yaml (which Navy Health's
sales lead and legal DO review and edit) or in a database table (which would
need a migration to change anyway, for data that never changes without a
schema version bump).

Sourced from PHOL-Lists-V3.1.xsd's enumerations and from a full scan of the
1 Oct 2026 extract (see docs/DATA_MODEL.md). Every list here is checked
against the database's CHECK constraints in migrations/versions/0002_market_data.py
by tests/test_reference.py — if the government adds a value, both need updating
together.
"""
from __future__ import annotations

from typing import Optional

# ── States ────────────────────────────────────────────────────────────────────
# "ALL" is a real value: some products are priced nationally with one row,
# not one row per state. Matched at query time with state = :state OR 'ALL'.
STATES = ("NSW", "VIC", "QLD", "SA", "WA", "TAS", "NT", "ACT", "ALL")

# ── Hospital tiers ──────────────────────────────────────────────────────────────
# The 4 government reform tiers (Basic < Bronze < Silver < Gold) each have a
# "Plus" variant: same regulatory minimum, extra inclusions beyond it. A Plus
# product is NOT a tier between the base and the next one up — it sits at the
# same regulatory floor with more on top. Rank is for sorting/comparison only;
# the enumeration order in the XSD itself is not a ranking.
HOSPITAL_TIER_RANK: dict[str, float] = {
    "Basic": 1.0, "BasicPlus": 1.5,
    "Bronze": 2.0, "BronzePlus": 2.5,
    "Silver": 3.0, "SilverPlus": 3.5,
    "Gold": 4.0,
}

# ── Accommodation ───────────────────────────────────────────────────────────────
# All 6 schema values; PrivateSharedPublic appears in the schema but was not
# seen in the 1 Oct 2026 extract (PrivatePublicShared was, with the words in
# the other order — both are real, distinct enum values).
ACCOMMODATION_TYPES = (
    "PrivateOrPublic", "PrivateSharedPublic", "PrivateSharedPublicShared",
    "Public", "PublicShared", "PrivatePublicShared",
)

# ── Excess and co-payment ────────────────────────────────────────────────────────
# The dollar figure means something different under each ExcessType, and
# stating it without the type is exactly the wrong-number-stated failure this
# system exists to prevent. PerAdmissionOnly wasn't seen in Hospital Open this
# month but is a valid schema value and must not be rejected if it appears.
EXCESS_TYPES = ("None", "PerAdmissionOnly", "LimitedPerYear", "LimitedPerPerson",
                "LimitedPerPersonPerPolicy")
COPAYMENT_TYPES = ("None", "Limited", "Unlimited")
AMBULANCE_COVER_TYPES = ("None", "EmergencyOnly", "EmergencyAndNonEmergency", "StateGovt")

# ── Brands ──────────────────────────────────────────────────────────────────────
# A fund may sell under up to 5 consumer-facing names (Fund.RelatedBrandNames;
# Product.Brands carries the code). 9 exist in the 1 Oct 2026 feed: "Frank"
# is GMHBA's GMH02, "Hunter Health Insurance" is CDH01, Teachers Health sells
# the same product as Teachers Health, UniHealth and Nurses & Midwives Health.
# A member says the brand; the resolver must land on the fund.

# ── Family type (derived, never stored as a government field) ──────────────────
# WhoIsCovered carries OnlyOnePerson + NumberOfAdults + which dependant
# categories are covered — there is no "Single/Couple/Family" field anywhere
# in the feed. Confirmed against the full Hospital Open file (31,864 rows,
# 1 Oct 2026): exactly 5 buckets occur, including the real but low-frequency
# "dependants only" case (NumberOfAdults="0": child-only cover, 127 rows).
FAMILY_TYPES = ("single", "couple", "family", "single_parent", "dependants_only")

# The 6 dependant categories a fund may cover, each with its own age band set
# at the FUND level (Fund.FundDependants), not the product level.
DEPENDANT_TITLES = ("Child", "NonClassified", "Student", "NonStudent",
                    "ConditionalNonStudent", "Disability")


def derive_family_type(*, only_one_person: bool, number_of_adults: Optional[int],
                       covered_dependant_titles: frozenset[str]) -> str:
    """The family_type a WhoIsCovered element implies. Pure and total: every
    combination the real data contains (see docs/DATA_MODEL.md) maps to one of
    FAMILY_TYPES. Raises on a shape the government data has never actually
    produced, rather than guessing — see that file for which raw rows exist."""
    if only_one_person:
        return "single"
    has_dependants = bool(covered_dependant_titles)
    if number_of_adults == 0:
        return "dependants_only"
    if number_of_adults == 2:
        return "family" if has_dependants else "couple"
    if number_of_adults == 1:
        if has_dependants:
            return "single_parent"
        # A lone adult, OnlyOnePerson=false, no dependants: not seen in the
        # 1 Oct 2026 extract. Could be a genuine new shape (not yet seen) or a
        # parse error upstream — surfaced, not silently guessed at.
        raise ValueError("NumberOfAdults=1, OnlyOnePerson=false, no dependants covered: "
                         "an unseen WhoIsCovered shape, not a known family_type")
    raise ValueError(f"unhandled WhoIsCovered shape: adults={number_of_adults}, "
                     f"dependants={sorted(covered_dependant_titles)}")


# Under one product name, 'family' is priced separately with and without
# adult non-student dependants — ~30% apart at Teachers Health (NTF "Top
# Hospital 500 (Gold)", NSW, family: $504.42 vs $655.75). family_type alone
# loses that, so the rate key carries the exact covered set as one canonical
# string, and two booleans give the UI the axis a member understands.
ADULT_DEPENDANT_TITLES = frozenset({"NonStudent", "ConditionalNonStudent"})


def dependant_scope(covered_dependant_titles: frozenset[str]) -> str:
    """The canonical key for a rate's dependant cover: sorted, '+'-joined,
    '' for none. "Child+NonClassified+Student". Stable across months and
    identical for identical cover, which is what a key needs."""
    unknown = covered_dependant_titles - set(DEPENDANT_TITLES)
    if unknown:
        raise ValueError(f"unknown DependantCover titles: {sorted(unknown)}")
    return "+".join(sorted(covered_dependant_titles))


def covers_adult_dependants(covered_dependant_titles: frozenset[str]) -> bool:
    return bool(covered_dependant_titles & ADULT_DEPENDANT_TITLES)


def excess_option(excess_type: Optional[str], per_person, per_admission) -> float:
    """The figure a rep picks an excess option by. Constant across a
    product's households even where the per-policy cap doubles; 0 for no
    excess. PerAdmissionOnly products carry it in per_admission."""
    if excess_type in (None, "None"):
        return 0.0
    if excess_type == "PerAdmissionOnly":
        return float(per_admission or 0)
    return float(per_person if per_person is not None else per_admission or 0)


# ── Hospital clinical categories ─────────────────────────────────────────────────
# The 38 MedicalService titles (typeMedicalServiceTitle in PHOL-Lists-V3.1.xsd).
# Each carries Cover (Covered/Restricted/NotCovered) per hospital variant; the
# wait is NOT per-category (see HOSPITAL_WAITING_PERIOD_TITLES below) except
# that PregnancyBirth's own wait is conditional on this category being covered.
CLINICAL_CATEGORIES = (
    "AssistedReproductive", "BackNeckSpine", "Blood", "BoneJointMuscle",
    "BrainNervousSystem", "BreastSurgery", "Cataracts",
    "ChemotherapyRadiotherapyImmunotherapy", "DentalSurgery", "Diabetes",
    "Dialysis", "DigestiveSystem", "EarNoseThroat", "Eye",
    "GastrointestinalEndoscopy", "Gynaecology", "HeartVascular",
    "HerniaAppendix", "HospitalPsychiatric", "ImplantationHearingDevices",
    "InsulinPumps", "JointReconstructions", "JointReplacements",
    "KidneyBladder", "LungChest", "MaleReproductive",
    "MiscarriageTerminationOfPregnancy", "PainManagement",
    "PainManagementWithDevice", "PalliativeCare", "PlasticReconstructiveSurgery",
    "PodiatricSurgery", "PregnancyBirth", "Rehabilitation", "Skin",
    "SleepStudies", "TonsilsAdenoidsGrommets", "WeightLossSurgery",
)
assert len(CLINICAL_CATEGORIES) == 38

CLINICAL_COVER_LEVELS = ("Covered", "Restricted", "NotCovered")

# Exactly 4 titles, confirmed exhaustively against all 31,864 Hospital Open
# rows: no per-category wait exists in the feed. PregnancyBirth is present
# only when that clinical category is Covered or Restricted.
HOSPITAL_WAITING_PERIOD_TITLES = ("SubAcute", "PreExisting", "PregnancyBirth", "Other")

# ── Extras (General Health) services ─────────────────────────────────────────────
# The 27 GeneralHealthService titles. Distinct taxonomy from the hospital
# categories above — do not confuse the two counts.
EXTRAS_SERVICES = (
    "Acupuncture", "AntenatalPostnatal", "Audiology", "ChineseHerbalMedicine",
    "Chiropractic", "DentalGeneral", "DentalMajor", "Dietetics", "Endodontic",
    "ExercisePhysiology", "GlucoseMonitor", "HealthManagement", "HearingAids",
    "HomeNursing", "NonPBS", "OccupationalTherapy", "Optical", "Orthodontic",
    "Orthoptics", "Orthotics", "Osteopathy", "Physiotherapy", "Podiatry",
    "Psychology", "RemedialMassage", "SpeechTherapy", "Vaccinations",
)
assert len(EXTRAS_SERVICES) == 27

# Benefit limits on extras are grouped: several services can share ONE cap
# (e.g. DentalGeneral + DentalMajor + Endodontic sharing a $1,700 annual
# limit at Navy Health). A per-service limit column would state that cap once
# per service and imply the member gets it that many times over — exactly
# backwards. See BenefitLimit.ServicesCombined in the XSD and
# docs/DATA_MODEL.md for the real NHB example this was found from.
SERVICE_COUNT_LIMIT_TYPES = ("Service", "Appliance")  # "2 appliances every 3 years" etc.

# The 48 example-benefit items (Benefit/@Item), all seen in the 1 Oct 2026
# extract. Dental items carry the ADA item number (012 periodic exam, 114
# scale and clean, 615 crown, 417 root canal, 881 braces); 322 extraction
# appears under both DentalGeneral and DentalMajor because funds classify it
# differently. Other services split Initial / Subsequent consultation or
# carry a single item. The labels are what a rep says and a glossary maps
# member words ("crown", "root canal", "physio") onto.
EXTRAS_ITEMS: dict[str, tuple[str, str]] = {   # item -> (service, plain label)
    "AcupunctureInitial": ("Acupuncture", "acupuncture, first visit"),
    "AcupunctureSubsequent": ("Acupuncture", "acupuncture, follow-up visit"),
    "AntenatalPostnatalInitial": ("AntenatalPostnatal", "antenatal or postnatal, first visit"),
    "AntenatalPostnatalSubsequent": ("AntenatalPostnatal", "antenatal or postnatal, follow-up visit"),
    "AudiologyInitial": ("Audiology", "audiology, first visit"),
    "AudiologySubsequent": ("Audiology", "audiology, follow-up visit"),
    "BloodGlucoseMonitor": ("GlucoseMonitor", "blood glucose monitor"),
    "ChineseHerbalMedicineInitial": ("ChineseHerbalMedicine", "Chinese herbal medicine, first visit"),
    "ChineseHerbalMedicineSubsequent": ("ChineseHerbalMedicine", "Chinese herbal medicine, follow-up visit"),
    "ChiropracticInitial": ("Chiropractic", "chiropractic, first visit"),
    "ChiropracticSubsequent": ("Chiropractic", "chiropractic, follow-up visit"),
    "DentalGeneral012PeriodicExam": ("DentalGeneral", "dental check-up (item 012)"),
    "DentalGeneral114ScaleClean": ("DentalGeneral", "scale and clean (item 114)"),
    "DentalGeneral121Fluoride": ("DentalGeneral", "fluoride treatment (item 121)"),
    "DentalGeneral322Extraction": ("DentalGeneral", "tooth extraction (item 322)"),
    "DentalMajor322Extraction": ("DentalMajor", "tooth extraction (item 322)"),
    "DentalMajor615FullCrownVeneered": ("DentalMajor", "crown (item 615)"),
    "DieteticsInitial": ("Dietetics", "dietitian, first visit"),
    "DieteticsSubsequent": ("Dietetics", "dietitian, follow-up visit"),
    "Endodontic417RootCanalTherapy": ("Endodontic", "root canal (item 417)"),
    "ExercisePhysiologyInitial": ("ExercisePhysiology", "exercise physiology, first visit"),
    "ExercisePhysiologySubsequent": ("ExercisePhysiology", "exercise physiology, follow-up visit"),
    "HealthManagement": ("HealthManagement", "health management program"),
    "HearingAid": ("HearingAids", "hearing aid"),
    "HomeNursingInitial": ("HomeNursing", "home nursing, first visit"),
    "HomeNursingSubsequent": ("HomeNursing", "home nursing, follow-up visit"),
    "NonPBSPrescription": ("NonPBS", "non-PBS prescription"),
    "OccupationalTherapyInitial": ("OccupationalTherapy", "occupational therapy, first visit"),
    "OccupationalTherapySubsequent": ("OccupationalTherapy", "occupational therapy, follow-up visit"),
    "OpticalMultiFocalLenses": ("Optical", "multifocal lenses"),
    "OpticalSingleVisionLenses": ("Optical", "single-vision lenses"),
    "Orthodontic881BracesUpperLowerPlusRetainer": ("Orthodontic", "braces, upper and lower, with retainer (item 881)"),
    "OrthopticsInitial": ("Orthoptics", "orthoptics, first visit"),
    "OrthopticsSubsequent": ("Orthoptics", "orthoptics, follow-up visit"),
    "Orthotics": ("Orthotics", "orthotics"),
    "OsteopathyInitial": ("Osteopathy", "osteopathy, first visit"),
    "OsteopathySubsequent": ("Osteopathy", "osteopathy, follow-up visit"),
    "PhysiotherapyInitial": ("Physiotherapy", "physiotherapy, first visit"),
    "PhysiotherapySubsequent": ("Physiotherapy", "physiotherapy, follow-up visit"),
    "PodiatryInitial": ("Podiatry", "podiatry, first visit"),
    "PodiatrySubsequent": ("Podiatry", "podiatry, follow-up visit"),
    "PsychologyInitial": ("Psychology", "psychology, first visit"),
    "PsychologySubsequent": ("Psychology", "psychology, follow-up visit"),
    "RemedialMassageInitial": ("RemedialMassage", "remedial massage, first visit"),
    "RemedialMassageSubsequent": ("RemedialMassage", "remedial massage, follow-up visit"),
    "SpeechTherapyInitial": ("SpeechTherapy", "speech therapy, first visit"),
    "SpeechTherapySubsequent": ("SpeechTherapy", "speech therapy, follow-up visit"),
    "VaccinationsService": ("Vaccinations", "vaccination"),
}
assert len(EXTRAS_ITEMS) == 48
assert all(svc in EXTRAS_SERVICES for svc, _ in EXTRAS_ITEMS.values())
