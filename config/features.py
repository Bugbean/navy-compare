"""The benefits a rep can compare, with the words a rep or member uses for each.

A feature key names one comparable benefit:

    price, excess, copayment                 the rate for the household
    tier, accommodation, known_gap           the hospital product
    wait:<Title>                             one of the four hospital waiting periods
    cover:<Category>                         one of the 38 clinical categories
    extras:<Service>                         whether an extras service is covered, and its wait
    limit:<Service>                          the annual limit that applies to an extras service
    item:<Item>                              the benefit paid for one extras item (a crown, a physio visit)
    ambulance

The labels are the plain words shown on the screen. The `words` are what a rep might type to find
a benefit ("hips", "root canal", "glasses"): the type-ahead and the request reader both use them.
Government names (CLINICAL_CATEGORIES and friends) live in config/reference.py and are not
repeated here; this file only adds the words people say.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from config.reference import (
    CLINICAL_CATEGORIES,
    EXTRAS_ITEMS,
    EXTRAS_SERVICES,
    HOSPITAL_WAITING_PERIOD_TITLES,
)

CATEGORY_LABELS: dict[str, str] = {
    "AssistedReproductive": "Assisted reproduction (IVF)",
    "BackNeckSpine": "Back, neck and spine",
    "Blood": "Blood",
    "BoneJointMuscle": "Bone, joint and muscle",
    "BrainNervousSystem": "Brain and nervous system",
    "BreastSurgery": "Breast surgery (medically necessary)",
    "Cataracts": "Cataracts",
    "ChemotherapyRadiotherapyImmunotherapy": "Chemotherapy, radiotherapy and immunotherapy",
    "DentalSurgery": "Dental surgery",
    "Diabetes": "Diabetes management",
    "Dialysis": "Dialysis for kidney failure",
    "DigestiveSystem": "Digestive system",
    "EarNoseThroat": "Ear, nose and throat",
    "Eye": "Eye (not cataracts)",
    "GastrointestinalEndoscopy": "Gastrointestinal endoscopy",
    "Gynaecology": "Gynaecology",
    "HeartVascular": "Heart and vascular system",
    "HerniaAppendix": "Hernia and appendix",
    "HospitalPsychiatric": "Hospital psychiatric services",
    "ImplantationHearingDevices": "Hearing device implants",
    "InsulinPumps": "Insulin pumps",
    "JointReconstructions": "Joint reconstructions",
    "JointReplacements": "Joint replacements",
    "KidneyBladder": "Kidney and bladder",
    "LungChest": "Lung and chest",
    "MaleReproductive": "Male reproductive system",
    "MiscarriageTerminationOfPregnancy": "Miscarriage and termination of pregnancy",
    "PainManagement": "Pain management",
    "PainManagementWithDevice": "Pain management with a device",
    "PalliativeCare": "Palliative care",
    "PlasticReconstructiveSurgery": "Plastic and reconstructive surgery (medically necessary)",
    "PodiatricSurgery": "Podiatric surgery",
    "PregnancyBirth": "Pregnancy and birth",
    "Rehabilitation": "Rehabilitation",
    "Skin": "Skin",
    "SleepStudies": "Sleep studies",
    "TonsilsAdenoidsGrommets": "Tonsils, adenoids and grommets",
    "WeightLossSurgery": "Weight loss surgery",
}

SERVICE_LABELS: dict[str, str] = {
    "Acupuncture": "Acupuncture",
    "AntenatalPostnatal": "Antenatal and postnatal",
    "Audiology": "Audiology",
    "ChineseHerbalMedicine": "Chinese herbal medicine",
    "Chiropractic": "Chiropractic",
    "DentalGeneral": "General dental",
    "DentalMajor": "Major dental",
    "Dietetics": "Dietitian",
    "Endodontic": "Endodontic (root canal)",
    "ExercisePhysiology": "Exercise physiology",
    "GlucoseMonitor": "Glucose monitor",
    "HealthManagement": "Health management programs",
    "HearingAids": "Hearing aids",
    "HomeNursing": "Home nursing",
    "NonPBS": "Non-PBS prescriptions",
    "OccupationalTherapy": "Occupational therapy",
    "Optical": "Optical (glasses and contacts)",
    "Orthodontic": "Orthodontic (braces)",
    "Orthoptics": "Orthoptics",
    "Orthotics": "Orthotics",
    "Osteopathy": "Osteopathy",
    "Physiotherapy": "Physiotherapy",
    "Podiatry": "Podiatry",
    "Psychology": "Psychology",
    "RemedialMassage": "Remedial massage",
    "SpeechTherapy": "Speech therapy",
    "Vaccinations": "Vaccinations",
}

WAIT_LABELS: dict[str, str] = {
    "PreExisting": "Wait: pre-existing conditions",
    "Other": "Wait: other hospital treatment",
    "SubAcute": "Wait: rehabilitation, psychiatric and palliative care",
    "PregnancyBirth": "Wait: pregnancy and birth",
}

# What people say, mapped to feature keys. Each phrase is matched word by word, so "hip" finds
# joint replacements and "root canal" finds the endodontic service and item.
SYNONYMS: dict[str, list[str]] = {
    "hip knee hips knees joint joints replacement": ["cover:JointReplacements"],
    "acl reconstruction knee reconstruction": ["cover:JointReconstructions"],
    "back neck spine spinal": ["cover:BackNeckSpine"],
    "baby birth pregnant pregnancy maternity obstetrics": ["cover:PregnancyBirth", "wait:PregnancyBirth"],
    "ivf fertility": ["cover:AssistedReproductive"],
    "cataract cataracts": ["cover:Cataracts"],
    "heart cardiac": ["cover:HeartVascular"],
    "cancer chemo chemotherapy radiotherapy oncology": ["cover:ChemotherapyRadiotherapyImmunotherapy"],
    "mental psychiatric psych": ["cover:HospitalPsychiatric", "extras:Psychology"],
    "rehab rehabilitation": ["cover:Rehabilitation"],
    "kidney dialysis": ["cover:Dialysis", "cover:KidneyBladder"],
    "weight bariatric lap band": ["cover:WeightLossSurgery"],
    "tonsils grommets adenoids": ["cover:TonsilsAdenoidsGrommets"],
    "dental dentist teeth tooth": ["extras:DentalGeneral", "extras:DentalMajor", "limit:DentalGeneral"],
    "check up checkup scale clean": ["item:DentalGeneral012PeriodicExam", "item:DentalGeneral114ScaleClean"],
    "crown crowns": ["item:DentalMajor615FullCrownVeneered"],
    "root canal": ["extras:Endodontic", "item:Endodontic417RootCanalTherapy"],
    "braces orthodontics": ["extras:Orthodontic", "item:Orthodontic881BracesUpperLowerPlusRetainer"],
    "physio physiotherapy": ["extras:Physiotherapy", "item:PhysiotherapyInitial", "limit:Physiotherapy"],
    "chiro chiropractor": ["extras:Chiropractic", "item:ChiropracticInitial"],
    "massage": ["extras:RemedialMassage", "item:RemedialMassageInitial"],
    "glasses contacts lenses optical eyewear": ["extras:Optical", "limit:Optical", "item:OpticalSingleVisionLenses"],
    "hearing aid aids": ["extras:HearingAids", "item:HearingAid"],
    "psychologist counselling": ["extras:Psychology", "item:PsychologyInitial"],
    "premium price cost monthly": ["price"],
    "excess": ["excess"],
    "co-payment copayment": ["copayment"],
    "ambulance": ["ambulance"],
    "tier gold silver bronze basic": ["tier"],
    "private room accommodation": ["accommodation"],
    "waiting wait waits": ["wait:PreExisting", "wait:Other", "wait:SubAcute", "wait:PregnancyBirth"],
}


@dataclass(frozen=True)
class Feature:
    key: str
    label: str
    group: str
    words: frozenset[str] = field(default_factory=frozenset)


GROUPS = ("Price", "Hospital", "Clinical categories", "Extras services", "Extras limits",
          "Extras benefits", "Ambulance")

_WORD = re.compile(r"[a-z0-9]+")


def _words(text: str) -> list[str]:
    return _WORD.findall(text.lower().replace("&", " and "))


def _build() -> dict[str, Feature]:
    out: dict[str, Feature] = {}

    def add(key: str, label: str, group: str) -> None:
        out[key] = Feature(key, label, group, frozenset(_words(label)))

    add("price", "Premium a month", "Price")
    add("excess", "Excess", "Price")
    add("copayment", "Co-payment", "Price")
    add("tier", "Hospital tier", "Hospital")
    add("accommodation", "Hospital accommodation", "Hospital")
    add("known_gap", "Known gap cover", "Hospital")
    for title in HOSPITAL_WAITING_PERIOD_TITLES:
        add(f"wait:{title}", WAIT_LABELS[title], "Hospital")
    for cat in CLINICAL_CATEGORIES:
        add(f"cover:{cat}", CATEGORY_LABELS[cat], "Clinical categories")
    for svc in EXTRAS_SERVICES:
        add(f"extras:{svc}", SERVICE_LABELS[svc], "Extras services")
        add(f"limit:{svc}", f"{SERVICE_LABELS[svc]}: annual limit", "Extras limits")
    for item, (_svc, label) in EXTRAS_ITEMS.items():
        add(f"item:{item}", label[0].upper() + label[1:], "Extras benefits")
    add("ambulance", "Ambulance cover", "Ambulance")

    extra: dict[str, set[str]] = {}
    for phrase, keys in SYNONYMS.items():
        for k in keys:
            extra.setdefault(k, set()).update(_words(phrase))
    for k, ws in extra.items():
        f = out[k]
        out[k] = Feature(f.key, f.label, f.group, f.words | frozenset(ws))
    return out


FEATURES: dict[str, Feature] = _build()


def item_service(key: str) -> str | None:
    """The extras service an item: or limit: or extras: key belongs to."""
    kind, _, name = key.partition(":")
    if kind == "item":
        return EXTRAS_ITEMS[name][0]
    if kind in ("extras", "limit"):
        return name
    return None


def search(query: str, limit: int = 10) -> list[Feature]:
    """Features whose words start with every word of the query, best first: a label that begins
    with the query, then a label match, then a match on the words people say."""
    terms = _words(query)
    if not terms:
        return []
    scored = []
    for f in FEATURES.values():
        label_words = _words(f.label)
        if not all(any(w.startswith(t) for w in f.words) for t in terms):
            continue
        in_label = sum(1 for t in terms if any(w.startswith(t) for w in label_words))
        starts = f.label.lower().startswith(query.strip().lower())
        scored.append((-(starts * 10 + in_label * 2), GROUPS.index(f.group), f.label, f))
    scored.sort(key=lambda s: s[:3])
    return [s[3] for s in scored[:limit]]
