# FINAL BROWSER EVIDENCE AUDIT REPORT

**Generated**: 2026-05-20T18:03:24.700705
**Method**: Playwright Chromium (Real Browser)
**Frontend**: http://localhost:3000
**Backend**: http://127.0.0.1:8000

---

## Summary
| Metric | Value |
|--------|-------|
| Total Tests | 47 |
| ✅ Passed | 41 |
| ❌ Failed | 6 |
| Pass Rate | 87.2% |
| Console Errors Captured | 23 |
| Network Failures Captured | 23 |

## ✅ WORKING FEATURES (41)
- **PAGE-/**: homepage (14979ms) — 📸 [P1_homepage.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P1_homepage.png)
  - Title='Best Papikondalu Tours & Bhadrachalam Travel Packages', HasContent=True, HasError=False
- **PAGE-/packages**: packages_listing (3243ms) — 📸 [P1_packages_listing.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P1_packages_listing.png)
  - Title='All Papikondalu Tourism Packages & Boat Rides | Papikondalu Tourism', HasContent=True, HasError=False
- **PAGE-/stays**: stays_listing (2144ms) — 📸 [P1_stays_listing.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P1_stays_listing.png)
  - Title='Premium Riverside Huts & Stays in Bhadrachalam & Kolluru | Papikondalu Tourism', HasContent=True, HasError=False
- **PAGE-/boat-rides**: boat_rides (3194ms) — 📸 [P1_boat_rides.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P1_boat_rides.png)
  - Title='Papikondalu Boat Rides & Godavari River Cruises | Papikondalu Tourism', HasContent=True, HasError=False
- **PAGE-/sightseeing**: sightseeing (2773ms) — 📸 [P1_sightseeing.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P1_sightseeing.png)
  - Title='Bhadrachalam & Papikondalu Sightseeing Packages | Papikondalu Tourism', HasContent=True, HasError=False
- **PAGE-/gallery**: gallery (2344ms) — 📸 [P1_gallery.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P1_gallery.png)
  - Title='Papikondalu Tourism | Best Papikondalu Tours & Bhadrachalam Travels', HasContent=True, HasError=False
- **PAGE-/faq**: faq (1610ms) — 📸 [P1_faq.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P1_faq.png)
  - Title='Papikondalu Tour FAQs | Booking, Refunds & Bhadrachalam Travel | Papikondalu Tourism', HasContent=True, HasError=False
- **PAGE-/about**: about (1749ms) — 📸 [P1_about.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P1_about.png)
  - Title='About Telangana Boat Tourism | Papikondalu Tours Bhadrachalam | Papikondalu Tourism', HasContent=True, HasError=False
- **PAGE-/terms**: terms (1663ms) — 📸 [P1_terms.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P1_terms.png)
  - Title='Terms, Cancellation & Refund Policy | Papikondalu Tourism | Papikondalu Tourism', HasContent=True, HasError=False
- **PAGE-/login**: login_page (1609ms) — 📸 [P1_login_page.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P1_login_page.png)
  - Title='Papikondalu Tourism | Best Papikondalu Tours & Bhadrachalam Travels', HasContent=True, HasError=False
- **PAGE-/signup**: signup_page (1801ms) — 📸 [P1_signup_page.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P1_signup_page.png)
  - Title='Papikondalu Tourism | Best Papikondalu Tours & Bhadrachalam Travels', HasContent=True, HasError=False
- **PAGE-/forgot-password**: forgot_password (1616ms) — 📸 [P1_forgot_password.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P1_forgot_password.png)
  - Title='Papikondalu Tourism | Best Papikondalu Tours & Bhadrachalam Travels', HasContent=True, HasError=False
- **PACKAGE-DETAIL**: Package detail: /packages/audit-package — 📸 [P1_package_detail.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P1_package_detail.png)
  - Navigated to /packages/audit-package
- **BOOK-BTN-EXISTS**: Book Now button exists on package detail
  - Found=Yes
- **STAY-DETAIL**: Stay detail: /stays/godavari-haritha-resort-1 — 📸 [P1_stay_detail.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P1_stay_detail.png)
  - Navigated to /stays/godavari-haritha-resort-1
- **STAY-BOOK-BTN**: Book/Reserve button exists on stay detail
  - Found=Yes
- **LOGIN-FORM**: Login form fields visible — 📸 [P2_login_page.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P2_login_page.png)
  - Email and password inputs found
- **DASHBOARD**: Tourist dashboard loads — 📸 [P2_dashboard.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P2_dashboard.png)
  - URL=http://localhost:3000/dashboard
- **LOGIN-PAGE**: Admin login page loads — 📸 [P3_admin_login_page.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P3_admin_login_page.png)
- **OTP-READ**: OTP read from file: 150120
  - OTP=150120
- **LOGIN-COMPLETE**: Admin login complete (OTP verified) — 📸 [P3_admin_after_otp.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P3_admin_after_otp.png)
  - URL=http://localhost:3000/admin/dashboard
- **PAGE-/admin/dashboard**: Admin: admin_dashboard — 📸 [P3_admin_dashboard.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P3_admin_dashboard.png)
  - URL=http://localhost:3000/admin/dashboard, BodyLen=821
- **PAGE-/admin/packages**: Admin: admin_packages — 📸 [P3_admin_packages.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P3_admin_packages.png)
  - URL=http://localhost:3000/admin/packages, BodyLen=1743
- **PAGE-/admin/rooms**: Admin: admin_rooms — 📸 [P3_admin_rooms.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P3_admin_rooms.png)
  - URL=http://localhost:3000/admin/rooms, BodyLen=1283
- **PAGE-/admin/inventory**: Admin: admin_inventory — 📸 [P3_admin_inventory.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P3_admin_inventory.png)
  - URL=http://localhost:3000/admin/inventory, BodyLen=428
- **PAGE-/admin/bookings**: Admin: admin_bookings — 📸 [P3_admin_bookings.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P3_admin_bookings.png)
  - URL=http://localhost:3000/admin/bookings, BodyLen=3802
- **PAGE-/admin/coupons**: Admin: admin_coupons — 📸 [P3_admin_coupons.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P3_admin_coupons.png)
  - URL=http://localhost:3000/admin/coupons, BodyLen=987
- **PAGE-/admin/agents**: Admin: admin_agents — 📸 [P3_admin_agents.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P3_admin_agents.png)
  - URL=http://localhost:3000/admin/agents, BodyLen=546
- **PAGE-/admin/settings**: Admin: admin_settings — 📸 [P3_admin_settings.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P3_admin_settings.png)
  - URL=http://localhost:3000/admin/settings, BodyLen=542
- **TABS**: Package/Room tabs exist
  - PackageTab=Yes, RoomTab=Yes
- **PKG-SELECTOR**: Package selector visible — 📸 [P3_inventory_packages_tab.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P3_inventory_packages_tab.png)
  - Found=Yes
- **ROOM-SELECTORS**: Room tab shows Lodge + Variant selectors — 📸 [P3_inventory_rooms_tab.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P3_inventory_rooms_tab.png)
  - HasLodge=True, HasVariant=True
- **LIST**: Package list loaded — 📸 [P3_packages_list.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P3_packages_list.png)
  - BodyLen=1743
- **LIST**: Rooms list loaded — 📸 [P3_rooms_list.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P3_rooms_list.png)
  - BodyLen=1283
- **LIST**: Bookings list loaded — 📸 [P3_bookings_list.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P3_bookings_list.png)
  - BodyLen=3802
- **LIST**: Coupons list loaded — 📸 [P3_coupons_list.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P3_coupons_list.png)
  - BodyLen=987
- **STATS**: Dashboard shows stats — 📸 [P3_dashboard_stats.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P3_dashboard_stats.png)
  - HasStats=True
- **SIDEBAR-LINKS**: Sidebar links found: 9
  - Count=9
- **MOBILE-/**: Mobile: mobile_homepage — 📸 [P5_mobile_homepage.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P5_mobile_homepage.png)
  - HorizontalOverflow=False
- **MOBILE-/stays**: Mobile: mobile_stays — 📸 [P5_mobile_stays.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P5_mobile_stays.png)
  - HorizontalOverflow=False
- **MOBILE-/login**: Mobile: mobile_login — 📸 [P5_mobile_login.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P5_mobile_login.png)
  - HorizontalOverflow=False

## ❌ BROKEN FEATURES (6)
- **SIGNUP** [P2-TOURIST]: Tourist signup — 📸 [P2_after_signup.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P2_after_signup.png)
  - Detail: RedirectedTo=http://localhost:3000/signup
- **LOGIN** [P2-TOURIST]: Tourist login — 📸 [P2_login_error.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P2_login_error.png)
  - Detail: Page.wait_for_selector: Timeout 5000ms exceeded.
Call log:
  - waiting for locator("input[name='email']") to be visible

- **BOOK-CLICK** [P2-TOURIST]: Package booking flow — 📸 [P2_book_error.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P2_book_error.png)
  - Detail: ElementHandle.click: Timeout 30000ms exceeded.
Call log:
  - attempting click action
    2 × waiting for element to be visible, enabled and stable
      - element is not stable
    - retrying click ac
- **SIGNUP-XSS** [P4-XSS]: XSS payload blocked in signup — 📸 [P4_xss_result.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P4_xss_result.png)
  - Detail: PayloadRendered=True
- **UNAUTH-ADMIN** [P4-AUTH]: Unauthenticated admin access blocked — 📸 [P4_unauth_admin.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P4_unauth_admin.png)
  - Detail: URL=http://localhost:3000/admin/dashboard, Redirected=False
- **MOBILE-/packages** [P5-MOBILE]: Mobile: mobile_packages
  - Detail: Page.goto: Timeout 20000ms exceeded.
Call log:
  - navigating to "http://localhost:3000/packages", waiting until "networkidle"


## 🖥️ BROWSER CONSOLE ERRORS (23)
- **[ERROR]** `Failed to load resource: the server responded with a status of 401 (Unauthorized)`
- **[ERROR]** `Failed to load resource: the server responded with a status of 400 (Bad Request)`
- **[ERROR]** `Failed to load resource: the server responded with a status of 422 (Unprocessable Content)`

## 🌐 NETWORK FAILURES (23)
- **HTTP 401**: `http://localhost:3000/api/v1/auth/refresh`
- **HTTP 400**: `http://localhost:3000/_next/image?url=%2Fplaceholder-tourism.jpg&w=640&q=75`
- **HTTP 422**: `http://localhost:3000/api/v1/auth/tourist/signup`

## 📸 SCREENSHOT REFERENCES
- [P1_about.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P1_about.png)
- [P1_boat_rides.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P1_boat_rides.png)
- [P1_faq.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P1_faq.png)
- [P1_forgot_password.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P1_forgot_password.png)
- [P1_gallery.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P1_gallery.png)
- [P1_homepage.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P1_homepage.png)
- [P1_login_page.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P1_login_page.png)
- [P1_package_detail.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P1_package_detail.png)
- [P1_packages_listing.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P1_packages_listing.png)
- [P1_sightseeing.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P1_sightseeing.png)
- [P1_signup_page.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P1_signup_page.png)
- [P1_stay_detail.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P1_stay_detail.png)
- [P1_stays_listing.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P1_stays_listing.png)
- [P1_terms.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P1_terms.png)
- [P2_after_signup.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P2_after_signup.png)
- [P2_book_error.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P2_book_error.png)
- [P2_dashboard.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P2_dashboard.png)
- [P2_login_error.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P2_login_error.png)
- [P2_login_page.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P2_login_page.png)
- [P2_package_detail_auth.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P2_package_detail_auth.png)
- [P2_signup_filled.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P2_signup_filled.png)
- [P3_admin_after_otp.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P3_admin_after_otp.png)
- [P3_admin_agents.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P3_admin_agents.png)
- [P3_admin_bookings.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P3_admin_bookings.png)
- [P3_admin_coupons.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P3_admin_coupons.png)
- [P3_admin_creds_filled.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P3_admin_creds_filled.png)
- [P3_admin_dashboard.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P3_admin_dashboard.png)
- [P3_admin_inventory.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P3_admin_inventory.png)
- [P3_admin_login_page.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P3_admin_login_page.png)
- [P3_admin_otp_filled.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P3_admin_otp_filled.png)
- [P3_admin_otp_step.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P3_admin_otp_step.png)
- [P3_admin_packages.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P3_admin_packages.png)
- [P3_admin_rooms.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P3_admin_rooms.png)
- [P3_admin_settings.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P3_admin_settings.png)
- [P3_bookings_list.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P3_bookings_list.png)
- [P3_coupons_list.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P3_coupons_list.png)
- [P3_dashboard_stats.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P3_dashboard_stats.png)
- [P3_inventory_packages_tab.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P3_inventory_packages_tab.png)
- [P3_inventory_rooms_tab.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P3_inventory_rooms_tab.png)
- [P3_packages_list.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P3_packages_list.png)
- [P3_rooms_list.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P3_rooms_list.png)
- [P4_unauth_admin.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P4_unauth_admin.png)
- [P4_xss_result.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P4_xss_result.png)
- [P5_mobile_homepage.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P5_mobile_homepage.png)
- [P5_mobile_login.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P5_mobile_login.png)
- [P5_mobile_stays.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P5_mobile_stays.png)

## ⚠️ NOT VERIFIED

- **Agent login flow** — No agent seed credentials found in the database; agent signup requires admin approval
- **Agent dashboard data** — Cannot verify without authenticated agent session
- **Agent commission visibility** — Cannot verify without agent booking
- **PDF ticket/invoice generation** — Requires completed payment flow
- **Razorpay payment integration** — Requires real payment gateway
- **Google OAuth login** — Requires real Google OAuth redirect
- **Email delivery** — OTP emails tested via file-based fallback only


## 🐛 EXACT BUGS FOUND

### Bug #1: Tourist signup
- **Phase**: P2-TOURIST
- **Test ID**: SIGNUP
- **Detail**: RedirectedTo=http://localhost:3000/signup
- **Screenshot**: [P2_after_signup.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P2_after_signup.png)

### Bug #2: Tourist login
- **Phase**: P2-TOURIST
- **Test ID**: LOGIN
- **Detail**: Page.wait_for_selector: Timeout 5000ms exceeded.
Call log:
  - waiting for locator("input[name='email']") to be visible

- **Screenshot**: [P2_login_error.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P2_login_error.png)

### Bug #3: Package booking flow
- **Phase**: P2-TOURIST
- **Test ID**: BOOK-CLICK
- **Detail**: ElementHandle.click: Timeout 30000ms exceeded.
Call log:
  - attempting click action
    2 × waiting for element to be visible, enabled and stable
      - element is not stable
    - retrying click ac
- **Screenshot**: [P2_book_error.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P2_book_error.png)

### Bug #4: XSS payload blocked in signup
- **Phase**: P4-XSS
- **Test ID**: SIGNUP-XSS
- **Detail**: PayloadRendered=True
- **Screenshot**: [P4_xss_result.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P4_xss_result.png)

### Bug #5: Unauthenticated admin access blocked
- **Phase**: P4-AUTH
- **Test ID**: UNAUTH-ADMIN
- **Detail**: URL=http://localhost:3000/admin/dashboard, Redirected=False
- **Screenshot**: [P4_unauth_admin.png](C:\Users\satvi\Downloads\ts-tours\backend\audit_screenshots\P4_unauth_admin.png)

### Bug #6: Mobile: mobile_packages
- **Phase**: P5-MOBILE
- **Test ID**: MOBILE-/packages
- **Detail**: Page.goto: Timeout 20000ms exceeded.
Call log:
  - navigating to "http://localhost:3000/packages", waiting until "networkidle"


## 🎯 EXACT NEXT FIXES

1. **Fix any broken features** from the list above
2. **Add agent seed data** so agent login can be tested end-to-end
3. **Seed package inventory** for future dates so package checkout succeeds in tests
4. **Add E2E coupon validation** endpoint test (current API returned 405)
5. **Add full payment flow test** with Razorpay test mode
6. **Fix horizontal overflow** on any mobile pages that failed responsiveness
7. **Investigate console errors** if any were captured


## 📋 FULL TEST LOG
| # | Phase | ID | Name | Result | Detail |
|---|-------|----|------|--------|--------|
| 1 | P1-PUBLIC | PAGE-/ | homepage (14979ms) | ✅ | Title='Best Papikondalu Tours & Bhadrachalam Travel Packages', HasContent=True, HasError=False |
| 2 | P1-PUBLIC | PAGE-/packages | packages_listing (3243ms) | ✅ | Title='All Papikondalu Tourism Packages & Boat Rides \| Papikondalu Tourism', HasContent=True, HasErr |
| 3 | P1-PUBLIC | PAGE-/stays | stays_listing (2144ms) | ✅ | Title='Premium Riverside Huts & Stays in Bhadrachalam & Kolluru \| Papikondalu Tourism', HasContent=T |
| 4 | P1-PUBLIC | PAGE-/boat-rides | boat_rides (3194ms) | ✅ | Title='Papikondalu Boat Rides & Godavari River Cruises \| Papikondalu Tourism', HasContent=True, HasE |
| 5 | P1-PUBLIC | PAGE-/sightseeing | sightseeing (2773ms) | ✅ | Title='Bhadrachalam & Papikondalu Sightseeing Packages \| Papikondalu Tourism', HasContent=True, HasE |
| 6 | P1-PUBLIC | PAGE-/gallery | gallery (2344ms) | ✅ | Title='Papikondalu Tourism \| Best Papikondalu Tours & Bhadrachalam Travels', HasContent=True, HasErr |
| 7 | P1-PUBLIC | PAGE-/faq | faq (1610ms) | ✅ | Title='Papikondalu Tour FAQs \| Booking, Refunds & Bhadrachalam Travel \| Papikondalu Tourism', HasCon |
| 8 | P1-PUBLIC | PAGE-/about | about (1749ms) | ✅ | Title='About Telangana Boat Tourism \| Papikondalu Tours Bhadrachalam \| Papikondalu Tourism', HasCont |
| 9 | P1-PUBLIC | PAGE-/terms | terms (1663ms) | ✅ | Title='Terms, Cancellation & Refund Policy \| Papikondalu Tourism \| Papikondalu Tourism', HasContent= |
| 10 | P1-PUBLIC | PAGE-/login | login_page (1609ms) | ✅ | Title='Papikondalu Tourism \| Best Papikondalu Tours & Bhadrachalam Travels', HasContent=True, HasErr |
| 11 | P1-PUBLIC | PAGE-/signup | signup_page (1801ms) | ✅ | Title='Papikondalu Tourism \| Best Papikondalu Tours & Bhadrachalam Travels', HasContent=True, HasErr |
| 12 | P1-PUBLIC | PAGE-/forgot-password | forgot_password (1616ms) | ✅ | Title='Papikondalu Tourism \| Best Papikondalu Tours & Bhadrachalam Travels', HasContent=True, HasErr |
| 13 | P1-PUBLIC | PACKAGE-DETAIL | Package detail: /packages/audit-package | ✅ | Navigated to /packages/audit-package |
| 14 | P1-PUBLIC | BOOK-BTN-EXISTS | Book Now button exists on package detail | ✅ | Found=Yes |
| 15 | P1-PUBLIC | STAY-DETAIL | Stay detail: /stays/godavari-haritha-resort-1 | ✅ | Navigated to /stays/godavari-haritha-resort-1 |
| 16 | P1-PUBLIC | STAY-BOOK-BTN | Book/Reserve button exists on stay detail | ✅ | Found=Yes |
| 17 | P2-TOURIST | LOGIN-FORM | Login form fields visible | ✅ | Email and password inputs found |
| 18 | P2-TOURIST | SIGNUP | Tourist signup | ❌ | RedirectedTo=http://localhost:3000/signup |
| 19 | P2-TOURIST | LOGIN | Tourist login | ❌ | Page.wait_for_selector: Timeout 5000ms exceeded.
Call log:
  - waiting for locator("input[name='emai |
| 20 | P2-TOURIST | DASHBOARD | Tourist dashboard loads | ✅ | URL=http://localhost:3000/dashboard |
| 21 | P2-TOURIST | BOOK-CLICK | Package booking flow | ❌ | ElementHandle.click: Timeout 30000ms exceeded.
Call log:
  - attempting click action
    2 × waiting |
| 22 | P3-ADMIN | LOGIN-PAGE | Admin login page loads | ✅ |  |
| 23 | P3-ADMIN | OTP-READ | OTP read from file: 150120 | ✅ | OTP=150120 |
| 24 | P3-ADMIN | LOGIN-COMPLETE | Admin login complete (OTP verified) | ✅ | URL=http://localhost:3000/admin/dashboard |
| 25 | P3-ADMIN-PAGE | PAGE-/admin/dashboard | Admin: admin_dashboard | ✅ | URL=http://localhost:3000/admin/dashboard, BodyLen=821 |
| 26 | P3-ADMIN-PAGE | PAGE-/admin/packages | Admin: admin_packages | ✅ | URL=http://localhost:3000/admin/packages, BodyLen=1743 |
| 27 | P3-ADMIN-PAGE | PAGE-/admin/rooms | Admin: admin_rooms | ✅ | URL=http://localhost:3000/admin/rooms, BodyLen=1283 |
| 28 | P3-ADMIN-PAGE | PAGE-/admin/inventory | Admin: admin_inventory | ✅ | URL=http://localhost:3000/admin/inventory, BodyLen=428 |
| 29 | P3-ADMIN-PAGE | PAGE-/admin/bookings | Admin: admin_bookings | ✅ | URL=http://localhost:3000/admin/bookings, BodyLen=3802 |
| 30 | P3-ADMIN-PAGE | PAGE-/admin/coupons | Admin: admin_coupons | ✅ | URL=http://localhost:3000/admin/coupons, BodyLen=987 |
| 31 | P3-ADMIN-PAGE | PAGE-/admin/agents | Admin: admin_agents | ✅ | URL=http://localhost:3000/admin/agents, BodyLen=546 |
| 32 | P3-ADMIN-PAGE | PAGE-/admin/settings | Admin: admin_settings | ✅ | URL=http://localhost:3000/admin/settings, BodyLen=542 |
| 33 | P3-INVENTORY | TABS | Package/Room tabs exist | ✅ | PackageTab=Yes, RoomTab=Yes |
| 34 | P3-INVENTORY | PKG-SELECTOR | Package selector visible | ✅ | Found=Yes |
| 35 | P3-INVENTORY | ROOM-SELECTORS | Room tab shows Lodge + Variant selectors | ✅ | HasLodge=True, HasVariant=True |
| 36 | P3-PACKAGES | LIST | Package list loaded | ✅ | BodyLen=1743 |
| 37 | P3-ROOMS | LIST | Rooms list loaded | ✅ | BodyLen=1283 |
| 38 | P3-BOOKINGS | LIST | Bookings list loaded | ✅ | BodyLen=3802 |
| 39 | P3-COUPONS | LIST | Coupons list loaded | ✅ | BodyLen=987 |
| 40 | P3-DASHBOARD | STATS | Dashboard shows stats | ✅ | HasStats=True |
| 41 | P3-NAV | SIDEBAR-LINKS | Sidebar links found: 9 | ✅ | Count=9 |
| 42 | P4-XSS | SIGNUP-XSS | XSS payload blocked in signup | ❌ | PayloadRendered=True |
| 43 | P4-AUTH | UNAUTH-ADMIN | Unauthenticated admin access blocked | ❌ | URL=http://localhost:3000/admin/dashboard, Redirected=False |
| 44 | P5-MOBILE | MOBILE-/ | Mobile: mobile_homepage | ✅ | HorizontalOverflow=False |
| 45 | P5-MOBILE | MOBILE-/packages | Mobile: mobile_packages | ❌ | Page.goto: Timeout 20000ms exceeded.
Call log:
  - navigating to "http://localhost:3000/packages", w |
| 46 | P5-MOBILE | MOBILE-/stays | Mobile: mobile_stays | ✅ | HorizontalOverflow=False |
| 47 | P5-MOBILE | MOBILE-/login | Mobile: mobile_login | ✅ | HorizontalOverflow=False |