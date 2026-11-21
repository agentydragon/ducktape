# Withings scale → Home Assistant

The half of this that cannot be code, plus why the integration was chosen over an
airlock OAuth provider.

## Why the core integration and not airlock

Withings does not implement RFC 6749. Its token endpoint requires a non-standard
`action=requesttoken` parameter in the POST body, and the response nests the token under a
`body` key alongside a `status` field. Airlock's `provider_type: oauth2` is a generic
compliant client, so Withings cannot be added there as a config-only provider the way Oura
was — it needs a provider type or a response-shape hook, and then something to fetch
measurements. The core `withings` integration absorbs all of that.

## Setup — UI only, because config_flow state lives in `.storage/`

1. **Withings developer account** → create an application, note ClientID and Secret.
   One developer account covers every profile.
2. **HA → Settings → Devices & Services → Add Integration → Withings**, entering those
   credentials as an application credential. Authorise as the profile whose data you want.
3. **Find the created entity id** and put it in
   [`packages/rai/withings.yaml`](packages/rai/withings.yaml), replacing
   `sensor.withings_weight` — it derives from the Withings profile name, so it is not
   predictable in advance.
4. `./deploy.sh`.

## One config entry is one Withings user

`config_flow.py` reads `userid` out of the **token response** and makes it the entry's
`unique_id`, so every entity under an entry belongs to exactly that user. Consequences:

- the same user cannot be added twice (`_abort_if_unique_id_configured`);
- re-auth must be the same person (`_abort_if_unique_id_mismatch`, `wrong_account`);
- a second person needs their **own** OAuth flow — a `userid` only arrives by someone
  authorising, so there is no sub-profile fan-out from one token.

**Gotcha, and it is upstream of HA:** the scale attributes a weigh-in **by weight**. A guest
reaches these entities only if Withings decided the reading was yours. Nothing in HA can
rescue a misattribution — give another regular user their own Withings profile.

## Webhooks are optional here

HA prefers a public HTTPS URL on :443 to register webhooks. The sensor with **no polling
fallback is the sleep binary sensor**, which comes from a sleep mat. Weight and body
composition poll fine on a 14-day window, so a scale-only setup loses nothing without one.
