# Wizz Air Internal API Spec

Living notes on the undocumented Wizz Air endpoints Flywizz talks to (or could talk
to). Reverse-engineered from poking the live API and the booking flow on
2026-09-01. Update whenever an endpoint's behaviour, params, or response shape
changes.

All endpoints answer JSON. All of them require a session handshake (below); most
of them also require a per-session CSRF token echoed back as a header.

---

## Hosts and version

| Host | Purpose |
|---|---|
| `be.wizzair.com/{version}/Api` | The entire API. Search, assets, booking, check-in, payment. |
| `www.wizzair.com` | The SPA. Only used to warm cookies and to read the current API version. |

The version is part of the path (`https://be.wizzair.com/29.14.0/Api/...`) and
moves roughly every sprint. The SPA embeds it in the page:

```
apiUrl:"https://be.wizzair.com/29.14.0/Api"
```

Flywizz scrapes that string from `https://www.wizzair.com/en-gb` on first use and
falls back to a pinned constant if the scrape fails. Old versions keep answering
for a while, so a stale pin degrades slowly rather than breaking hard.

---

## Session handshake

Two steps, in order, before anything else works:

1. `GET https://www.wizzair.com/en-gb` — warms CloudFront / Akamai cookies.
2. `GET {base}/userSession/new` — returns `{}` and sets two cookies:
   `ASP.NET_SessionId` and `RequestVerificationToken` (32 chars).

From then on **every** request must carry the token as a header:

```
X-RequestVerificationToken: <value of the RequestVerificationToken cookie>
```

The token rotates: the server may reissue the cookie on any response. Re-read the
cookie jar before each request rather than caching the value.

Omitting or stale-ing the header does **not** produce a 401. It produces:

```json
{"handlerError": "InvalidProtocol"}
```

with status 400, on every endpoint. If you see `InvalidProtocol` everywhere,
the token is wrong, not your payload.

---

## Bot gate (Kasada)

A subset of endpoints sits behind Kasada. They answer `429` with a **zero-byte
body** and these response headers:

```
x-kpsdk-ct, x-kpsdk-r
set-cookie: ak_bm_vw_1.1=..., ak_bm_vw_1.1-ssn=...
```

The browser passes by sending `x-kpsdk-ct`, `x-kpsdk-v`, `x-kpsdk-h` and
`x-kpsdk-cd` headers computed by an obfuscated JS VM loaded from the page. The SPA
registers the gate for `*/Api/*`, but enforcement is per-endpoint.

Gated (confirmed 429 from plain HTTP, 200 from the browser):

- `search/search`
- `booking/seatmap`
- `booking/ancillaries`
- `booking/passengers`

Not gated (everything else probed, including all of `asset/*`,
`search/timetableV2`, `search/flightDates`, `asset/farechart`).

Flywizz does not attempt to solve Kasada. It raises `BotGateError` and offers
`WizzairTransport(kasada_headers=...)` so a caller who already has a live browser
session can hand the headers in.

**Consequence for SDK design:** the priced-inventory surface reachable without a
browser is `search/timetableV2` + `asset/farechart`, not `search/search`. Both
return real prices; neither returns fare bundles or sell keys.

---

## Error vocabulary

| Status + body | Means |
|---|---|
| `400 {"handlerError":"InvalidProtocol"}` | Missing/stale `X-RequestVerificationToken`, or the body failed model binding. |
| `400 {"validationCodes":[...]}` | Endpoint reached, DTO bound, business validation failed. The codes name the missing fields; useful for schema discovery. |
| `400 {"validationCodes":["SessionMismatch"]}` | Endpoint needs an active *booking* session (a flight selected), not just a browsing session. |
| `401 {"message":"Authorization has been denied for this request."}` | Needs a logged-in customer. |
| `404 {"message":"No HTTP resource was found ..."}` | Route does not exist. |
| `405 {"message":"...does not support http method 'GET'."}` | Route exists, wrong verb. Handy oracle for enumeration. |
| `429` with empty body | Kasada bot gate. Retrying will not help. |

---

## `GET /Api/asset/map`

The full route network. Source of truth for stations, coordinates, currencies and
which city flies to which.

**Query params**: `languageCode` (e.g. `en-gb`). Optional; affects `shortName`.

**Response**

```json
{
  "cities": [
    {
      "iata": "TIA",
      "mac": null,
      "shortName": "Tirana",
      "countryName": "Albania",
      "countryCode": "AL",
      "currencyCode": "ALL",
      "latitude": 41.415,
      "longitude": 19.7208,
      "aliases": ["Tirana"],
      "categories": [7, 8, 9, 0],
      "rank": 1,
      "isExcludedFromGeoLocation": false,
      "isFakeStation": false,
      "connections": [
        {
          "iata": "AGP",
          "operationStartDate": "2026-09-02T17:25:00",
          "rescueEndDate": "2026-09-01T20:20:45.849438+01:00",
          "isDomestic": false,
          "isNew": false,
          "isConnected": false,
          "isDirectFlight": true
        }
      ]
    }
  ],
  "javascript": null
}
```

196 cities at time of writing. `countryCode` is **uppercase** ISO2 here (the
opposite of Ryanair). `currencyCode` on the departure city is what prices come
back in; see below.

`isConnected: true` marks a Wizz-operated self-transfer connection rather than a
direct flight; `isDirectFlight` is the flag you actually want.

---

## `GET /Api/search/flightDates`

Which dates a route operates in a window. No prices, very cheap.

**Query params**

| Name | Required | Notes |
|---|---|---|
| `departureStation` | yes | Uppercase IATA. |
| `arrivalStation` | yes | Uppercase IATA. |
| `from` | yes | ISO date. |
| `to` | yes | ISO date. |

**Response**

```json
{"flightDates": ["2026-11-01T00:00:00", "2026-11-02T00:00:00"]}
```

Omitting the window returns `{"validationCodes":["InvalidFromDate"]}`.

---

## `POST /Api/search/timetableV2`

The workhorse. Cheapest fare per day for one or two legs across a date window.
Not bot-gated, so this is the priced surface a headless client can actually use.

**Body**

```json
{
  "flightList": [
    {"departureStation": "BUD", "arrivalStation": "LTN", "from": "2026-11-01", "to": "2026-11-30"},
    {"departureStation": "LTN", "arrivalStation": "BUD", "from": "2026-11-01", "to": "2026-11-30"}
  ],
  "priceType": "regular",
  "adultCount": 1,
  "childCount": 0,
  "infantCount": 0
}
```

`priceType` is `regular` or `wdc` (Wizz Discount Club pricing).
The first `flightList` entry becomes `outboundFlights`, the second `returnFlights`.

**Response**

```json
{
  "outboundFlights": [
    {
      "departureStation": "BUD",
      "arrivalStation": "LTN",
      "departureDate": "2026-11-01T06:00:00",
      "price": {"amount": 23016.0, "currencyCode": "HUF",
                "exchangedAmount": null, "exchangedCurrencyCode": null},
      "originalPrice": {"amount": 25190.0, "currencyCode": "HUF"},
      "priceType": "price",
      "departureDates": [
        {"date": "2026-11-01T06:00:00", "isCheapestOfTheDay": true},
        {"date": "2026-11-01T08:30:00", "isCheapestOfTheDay": false}
      ],
      "hasMacFlight": true,
      "appliedCouponCode": null
    }
  ],
  "returnFlights": []
}
```

One entry per operating day. `departureDates` lists every departure that day, so
you get the timetable and the cheapest price in a single call. `price` is the
cheapest of the day; `originalPrice` is the pre-discount price.

The legacy `search/timetable` route still exists and takes the same body.

---

## `POST /Api/asset/farechart`

The price strip above the search results: cheapest price per day in a window
centred on a date. Cheaper and narrower than `timetableV2`.

**Body**

```json
{
  "flightList": [{"departureStation": "BUD", "arrivalStation": "LTN", "date": "2026-11-10"}],
  "priceType": "regular",
  "adultCount": 1,
  "childCount": 0,
  "infantCount": 0,
  "dayInterval": 3
}
```

`dayInterval` must be >= 3 (`DayIntervalMustBeGreaterOrEqualTo3`) and is the
half-window: `dayInterval: 3` returns `date - 3 .. date + 3`.

**Response**

```json
{
  "outboundFlights": [
    {
      "departureStation": "BUD",
      "arrivalStation": "LTN",
      "date": "2026-11-07T00:00:00",
      "price": {"amount": 9802.0, "currencyCode": "HUF"},
      "priceType": "price",
      "classOfService": "RS",
      "hasMacFlight": false
    }
  ],
  "returnFlights": []
}
```

`priceType` is `price` when there is inventory; days with no flight come back
with a null price. `classOfService` is the booking class the quote came from.

---

## `POST /Api/search/search` — **bot-gated**

The real availability call: every departure with full fare bundles and the sell
keys the booking flow needs.

**Body**

```json
{
  "isFlightChange": false,
  "flightList": [
    {"departureStation": "BUD", "arrivalStation": "LTN", "departureDate": "2026-11-14T00:00:00"},
    {"departureStation": "LTN", "arrivalStation": "BUD", "departureDate": "2026-11-16T00:00:00"}
  ],
  "adultCount": 1,
  "childCount": 0,
  "infantCount": 0,
  "wdc": true
}
```

Dates are full ISO datetimes at midnight, not bare dates.

**Response top-level keys**

`outboundFlights`, `returnFlights`, `outboundBundles`, `returnBundles`,
`outboundCo2Emission`, `returnCo2Emission`, `currencyCode`,
`arrivalStationCurrencyCode`, `isDomestic`, `isGroup`, `discountType`,
`discountPercentage`, `bookingCurrencyRenewalPrice`, `connectedFlight`,
`isPrivilegePassAllowed`, `isOutboundAirportCheckinFree`,
`isReturnAirportCheckinFree`.

**Flight**

`departureStation`, `arrivalStation`, `aircraftName`, `carrierCode`,
`operatingCarrierCode`, `flightNumber`, `opSuffix`, `flightSellKey`,
`departureDateTime`, `departureTimeUtcOffset`, `arrivalDateTime`,
`arrivalTimeUtcOffset`, `duration`, `fares`, plus a family of
`*LimitExceeded` booleans (`infant`, `wheelchair`, `oxy`, `sportsEquipment`,
`autoCheckIn`), `isRequest`, `isLease`.

**Fare**

`fareSellKey`, `bundle`, `isWdc`, `isFamily`, `soldOut`, `promotion`,
`fareDiscountType`, and a large price family: `basePrice`, `fullBasePrice`,
`discountedPrice`, `discountPrice`, `pastPrice`, `administrationFeePrice`,
`distributionFeePrice`, `discountedBundlePrice`, `originalBundlePrice`,
`bundleDiscountPrice`, `baseFarePrice`, `discountedFarePrice`,
`baseFarePriceWithAdministrationFee`,
`discountedFarePriceWithAdministrationFee`, `flightChangeFeePrice`,
`fareLockPrice`, `flightPriceDetail`, `passengerFlightPriceDetails`,
`fareCouponDiscountPrice`, `fareDiscountPrice`,
`discountedPriceWithoutFamilyDiscount`,
`couponIncludedDiscountedPriceWithoutFamilyDiscount`,
`groupAdministrationFeePrice`.

`flightSellKey` / `fareSellKey` are the handles the rest of the booking flow
(`booking/select`, `booking/seatmap`, `booking/ancillaries`) takes.

---

## `POST /Api/asset/flightinformation`

Live flight status. Filter by flight number **or** by route, never both
(`ExactlyOneFilterRequired`).

**Body (by flight number)**

```json
{"carrierCode": "W6", "flightNumber": "6201", "date": "2026-11-10"}
```

**Response**

```json
{
  "flightInformationResponseItems": [
    {
      "flightId": 45750213,
      "carrierCode": "W4",
      "flightNumber": 6201,
      "opSuffix": null,
      "originalDepartureAirport": "FCO",
      "departureAirport": "FCO",
      "originalArrivalAirport": "CHQ",
      "arrivalAirport": "CHQ",
      "operationDay": "2026-09-04T00:00:00",
      "scheduledDepartureTime": "..."
    }
  ]
}
```

The `date` in the request is advisory: the endpoint answers with the next
operating days it knows about. `carrierCode` in the response can differ from the
request (`W6` Hungary vs `W4` Malta vs `W9` UK are separate AOCs).

---

## Static asset endpoints (GET, no body)

| Path | Returns |
|---|---|
| `asset/currencies` | `{"currencies": ["HUF","EUR", ...]}` — 20 supported ISO 4217 codes. |
| `asset/country` | `{"countries":[{"code","name","isEu","isSchengen","phonePrefix"}]}` — ~250 entries, uppercase ISO2. |
| `asset/cultures` | `{"cultures":[{"code":"hu-HU","name":"Magyar","currencyCode":["HUF"]}]}` |
| `asset/municipalities` | Spanish municipality → station mapping (resident-discount eligibility). |
| `asset/communities` | Spanish autonomous communities. |
| `asset/serviceFees` | **POST** `{"currencyCodes":["EUR"]}` — the full published fee table. |
| `asset/wdcPrice` | Wizz Discount Club membership tiers and their guaranteed minimum discounts. |
| `asset/privilegePass` | Privilege Pass monthly price. |
| `asset/insurance` | Per-country insurance language availability. |
| `asset/callCenterInformation` | Per-country call-centre numbers and hours. |
| `asset/locationInfo` | `{"akamaiHeaderValues":{"userCountryCode","userLocation"}}` — geo-IP of the caller. |
| `asset/emergencyNews` | `{"isEmergency": false, "emergencyNews": null}` |
| `asset/breakingnews` | `{"breakingNews": []}` |
| `asset/analytics` | Ancillary product-code → display-name map, plus `exchangeRate`. |
| `asset/disruptionCreditInformation` | Current credit-back bonus percentages. |
| `asset/importantInfo` | **POST**. Route-level regulatory labels. |
| `asset/yellowRibbon` | Empty 200 unless a campaign is live. |

`asset/culture` is **POST** `{"languageCode": "en-gb"}` and returns `{}`; it sets
the session language. It does **not** change the price currency (see below).

---

## Currency

Prices come back in the **departure station's** `currencyCode` from `asset/map`.
Verified: `BUD→LTN` gives HUF, `LTN→BUD` gives GBP, `WAW→LTN` gives PLN,
`EIN→BUD` gives EUR.

There is no server-side currency override. Body fields, query params, cookies and
headers named `currency` / `currencyCode` / `X-Requested-Currency` were all
tried and all ignored. The `exchangedAmount` / `exchangedCurrencyCode` fields on
every `Price` are the hook for the SPA's client-side MCP conversion and stay
`null` for anonymous sessions.

Flywizz therefore reports the native currency and leaves conversion to the caller.

---

## Full route table

Extracted from the SPA bundle's endpoint-constant block. `system/*` entries in the
same block are client-side store actions, not HTTP routes, and are excluded.

**search** — `search`*, `timetableV2`, `flightDates`, `FlightDatesMultiArrival`,
`dohopFlightDates`, `ConnectedFlights`, `SmartSearchCheapFlightsV2`,
`wizzDiscountClub`†, `additionalFees`†, `CouponPreValidate`

**asset** — `map`, `farechart`, `flightinformation`, `currencies`, `country`,
`culture`, `cultures`, `serviceFees`, `wdcPrice`, `municipalities`,
`communities`, `locationInfo`, `importantInfo`, `breakingnews`,
`emergencyNews`, `analytics`, `insurance`, `privilegePass`, `yellowRibbon`,
`callCenterInformation`, `initFeatureToggling`, `BankTransferFields`,
`checkMobileBoardingPassForStation`, `disruptionCreditInformation`

**booking** — `select`, `summary`, `itinerary`, `priceDetails`‡, `passengers`*,
`contact`‡, `phoneContact`, `ancillaries`*, `seatmap`*, `ancillaryPromoDetails`,
`airportparking`, `insurance`, `coupon`, `invoice`, `invoiceList`, `refund`,
`cancellation`, `cancellationFlow`, `modifyBooking`, `nameChange`,
`GroupNameChange`, `moveInfant`, `passengerRemoval`, `passengerRemovalInfo`,
`passengerRemovalStatus`, `VoluntaryFlightChange`, `VoluntaryFlightChangeStatus`,
`flightChangeInformation`, `divideFlightChangeServices`, `divideStatus`,
`acceptChange`, `acceptGcc`, `farelockfinalize`, `calculateBundleUpsell`,
`BundleUpsellSelect`, `lostServices`, `premiumSeatRefund`, `privilegePass`,
`wizzDiscountClub`, `WdcMembershipCutStatus`, `loggedInUserIsOwner`,
`checkIfDoubleBookingExists`, `groupItinerary`, `sendGroupRequest`,
`uploadPassengersCsv`, `travelAgencyBookingFindFlight`,
`TravelAgencyBookingAddContactData`, `captchaCheck`, `informationBanner`,
`BoardingCardBanners`, `CarTrawlerFrame`, `analytics`

**checkin** — `checkinFlow`, `CheckinParams`, `passengers`, `passengerLocators`,
`travelDocuments`, `travelDocumentScanning`, `contactData`,
`contactDataValidation`, `confirmContactData`, `changesToAccept`,
`overbookedQuestion`, `confirmCheckIn`, `successfulCheckIn`, `boardingCard`,
`googlewallet`, `qrScanning`, `warning`

**customer** ‡ — `login`, `logout`, `autologin`, `registration`,
`agencyRegistration`, `profile`, `PersonalDetails`, `ContactData`,
`BillingDetails`, `TravelDocument`, `changepassword`, `forgotPassword`,
`validateForgotPassword`, `emailValidation`, `emailVerification`,
`reSendTwoStepVerificationEmail`, `verifyCaptcha`, `consent`, `datainquiry`,
`mybookings`, `yourNextFlight`, `customeraccounthistory`, `voucherhistory`,
`storedCreditCard`, `newsletter`, `newslettersubscription`, `feedback`,
`updatemobilephone`, `wizzFreeClub`, `getTravelPartners`, `addTravelPartners`,
`updateTravelPartner`, `deleteTravelPartner`, `GetCustomerPriceAlerts`,
`AddCustomerPriceAlert`, `DeleteCustomerPriceAlert`, `AddUpcomingFlight`,
`DeleteUpcomingFlight`

**payment** ‡ — `creditCard`, `storedCreditCard`, `applepay`, `googlepay`,
`revolutpay`, `revolut-pay-result`, `ideal`, `trustly`, `bankTransfer`,
`paybylink`, `voucher`, `customerAccount`, `farelock`, `paymentOptions`,
`cardProcessingFee`, `polling`, `empty`, `validate-apple-pay-merchant`

**claim** — `claim`, `claimDetails`, `MyClaims`, `checkRecordLocator`,
`refundCompensation`, `supplementclaim`, `DSRClaim`, `claimLanguages`,
`airports`, and the `Trigger*` / `Poll*` async job pairs

**other** — `voucher/{voucher,select,itinerary,mail}`,
`ancillary/{fasttrack,lounge}`, `car/{search,SearchLocation}`, `hotel/search`,
`chatbot/getParameters`, `userSession/{new,keepAlive}`

`*` bot-gated · `†` needs a booking session · `‡` needs a logged-in customer

---

## Behavioural quirks worth remembering

- **The token rotates.** Re-read `RequestVerificationToken` from the cookie jar
  before every request. Caching it once produces `InvalidProtocol` on request two.
- **`InvalidProtocol` is not a payload error most of the time.** Check the token
  before you debug your body.
- **404 vs 405 is a free route oracle.** `405` means the path exists; that is how
  the route table above was cross-checked against the SPA constants.
- **Country codes are uppercase** in `asset/map` and `asset/country`. Opposite of
  Ryanair, so do not copy that habit across.
- **`search/search` dates are datetimes**, `timetableV2` and `farechart` dates are
  bare ISO dates. The API is not consistent about this.
- **Do not retry a 429.** It is the bot gate, not backpressure. Exponential backoff
  just adds load and still fails.
- **`SmartSearchCheapFlightsV2` answers `{"items": []}` for every payload tried.**
  It is reachable and un-gated, so the shape is wrong rather than the endpoint
  being dead. Unresolved.
- **`asset/farechart` with two `flightList` entries** returns only
  `outboundFlights` populated; the return strip appears to need a booking session.
