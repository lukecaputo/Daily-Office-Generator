# Daily Office eBook Generator

## Introduction
Generates the Daily Office for every day of the year as pre-formatted EPUB for eReader use.


## Source
Content is retrieved from venite.app. All credit for this original source goes to Rev. Greg Johnston.


## Formatting Rules
  - Language: English, Rite II, BCP 1979 calendar
  - Bible: NRSV, Psalter: 1979 BCP
  - Lectionary: Daily Office Lectionary
  - Canticles: 1979 Rite II Table of Suggested Canticles
  - Confession: Short form, Rite II, Lay/Deacon absolution
  - Lord's Prayer: Traditional
  - Suffrages: A
  - Invitatory: Venite (BCP)
  - Include Collects for Minor Feasts: yes


## Dependencies
  `pip install playwright beautifulsoup4 ebooklib lxml`

  `playwright install chromium`


## Usage
  `python generate_bcp_ebook.py`
  
    "--year" : Generates the entire year unless a month is specified. Excluding this argument results in the current year. 
    "--month" : Generates a specific month from the default year, unless the year is specified. Accepts a number (1-12) or a full/abbreviated month name. 
    "--date" : Generates a specific date (YYYY-MM-DD). Unable to be combined with the year or month argument. 
    "--season" : Generates the entire liturgical season (advent, christmas or xmas, epiphany, lent, holyweek or holy-week, easter or eastertide, ordinary-time or ordinarytime or pentecost). 
    "--package" : Providing the argument "full" will generate a full year, 12 months, 365 days, and each season as a total package for the given year. The "--year" argument must be used.


## Output
  `bcp_daily_office_2026.epub`

  `bcp_daily_office_2026_March.epub`

  `bcp_daily_office_2026_03_15.epub`


## Kindle
Use Calibre to convert from EPUB to AZW3. I'm including these in the generated files alongside the EPUB files. 

On Kindle Paperwhite 11, the following settings look the best:

  - Font: Bookerly, Bold 0, Size 3
  - Layout: Portrait, Narrow Margins, Narrow Spacing
  - Reading Progress: None

## Other Devices
Your mileage may vary. I tried using this on my Xteink X4 and it looks pretty bad.