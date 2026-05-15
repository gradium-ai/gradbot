"""Draft polite viewing-request messages in French or English."""

from __future__ import annotations

from typing import Any


def draft_viewing_request_text(
    *,
    user_full_name: str | None,
    renter_phone: str | None,
    listing: dict[str, Any],
    profile: dict[str, Any] | None,
    language: str = "en",
) -> tuple[str, str]:
    """Returns (subject, body). Sending is NOT implemented — drafts only."""
    title = listing.get("title") or "your listing"
    address = listing.get("address_text") or ""
    arr = listing.get("arrondissement")
    addr_label = address or (f"Paris {arr:02d}" if arr else "")

    name = user_full_name or "the prospective tenant"
    phone = renter_phone or ""

    if language == "fr":
        subject = f"Demande de visite — {title}"
        body_lines = [
            "Bonjour,",
            "",
            f"Je me permets de vous contacter au sujet de votre annonce « {title} »"
            + (f" située à {addr_label}." if addr_label else "."),
            "",
            "Mon profil correspond à ce bien et je serais très intéressé(e) "
            "par une visite à votre convenance.",
        ]
        if profile and profile.get("max_rent_including_charges_eur"):
            body_lines.append(
                f"Mon budget maximum est de {profile['max_rent_including_charges_eur']} €"
                " charges comprises, ce qui correspond à votre offre."
            )
        body_lines.extend(
            [
                "",
                "Je peux fournir un dossier complet (justificatifs de revenus, garant, "
                "pièce d'identité) sur simple demande.",
                "",
                "Pourriez-vous me proposer un créneau pour une visite ?",
                "",
                "Cordialement,",
                f"{name}" + (f" — {phone}" if phone else ""),
            ]
        )
        body = "\n".join(body_lines)
    else:
        subject = f"Viewing request — {title}"
        body_lines = [
            "Hello,",
            "",
            f"I'm reaching out about your listing \"{title}\""
            + (f" in {addr_label}." if addr_label else "."),
            "",
            "It matches what I'm looking for and I'd love to schedule a viewing at your convenience.",
        ]
        if profile and profile.get("max_rent_including_charges_eur"):
            body_lines.append(
                f"My budget is up to €{profile['max_rent_including_charges_eur']} including charges, "
                "which fits your listing."
            )
        body_lines.extend(
            [
                "",
                "I can provide a complete rental file (proof of income, guarantor, ID) on request.",
                "",
                "Could you propose a time for a viewing?",
                "",
                "Best regards,",
                f"{name}" + (f" — {phone}" if phone else ""),
            ]
        )
        body = "\n".join(body_lines)

    return subject, body
