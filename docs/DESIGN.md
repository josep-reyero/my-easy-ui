# Visual language

The dashboard page is something a person opens every morning to see how things are going. It
should feel like a calm, well-set almanac of their own numbers: quiet chrome, big honest figures,
charts without decoration.

**Colour.** A cool, misty neutral base with one accent, the app's own (`[ui] accent` in
`app.toml`). The accent tints the page very slightly, so every app feels like itself, and it marks
the few things that matter: the current page, the main data series, links, focus rings. Light and
dark follow the system setting. Neutrals are hex tokens in `style.css` (`--base-*`). `theme.js`
mixes in the accent in OKLab and nudges it until text reaches 4.5:1 contrast and chart marks reach
3:1, whatever colour the app picks. Further chart series turn around the hue circle at the same
calm lightness. Errors use one muted red. Nothing else is coloured.

**Type.** System fonts only. Page and section headings use a book serif (Iowan Old Style, then
Palatino and its look-alikes). Everything else uses the system sans. Numbers are the main thing on
the page: stats are large, light and tabular, with the unit set smaller beside them (`849.15 km`).
Labels are sentence case and never all caps. A date or a word in a stat is set smaller than a
number. Times are 24-hour ("Checked 16:03"), like the English dates ("4 May 2026").

**Changes.** The small change under a stat is neutral grey: a rise is not always good news. When
the dashboard says which way is good (`better: higher | lower`), a good change takes the accent
colour and a bad one the muted red, and screen readers hear "up (better)" or "down (worse)".

**Layout.** One left-aligned column, up to 76 rem wide, with generous space between rows. A row's
components share its width and wrap when there is no room, so on a phone everything stacks;
a row of stats becomes an even two-column grid (an odd last stat takes the full width). The page
opens on the first dashboard of the app's order (`[ui] dashboards`); "All" in the menu lists
every dashboard with its description. Stats
sit straight on the page under a hairline. Charts and tables sit on slightly raised panels with no
shadow. Text is plain prose, at most 66 characters wide.

**Charts.** Quiet axes: no tick marks, a faint baseline, three or four faint gridlines, small grey
labels, and round time steps (0, 30 min, 1 h). One short form per axis (15k, 10k, 5k, 0). There is
no floating tooltip. On a date axis a readout in the chart heading shows the latest value, and
while the pointer moves it shows the value under it; on a category axis ("run", "walk") it only
shows the value under the pointer, because no category is "the latest". With
several series, a small legend carries the values. Every chart can switch to a table ("View as
table", newest first), which also serves screen readers.

**States.** Loading keeps the space with a soft placeholder that only fades in when loading takes a
moment. On later loads the old content stays and dims. Empty and error states say what happened
and what to do, in the component's own space. "No data yet" is only for a query without
controls; when controls narrow it down, it says "Nothing matches these settings" and offers to
reset them. "Checked" only moves when a load worked; when the server does not answer, the page
says so. A data problem that affects the whole page shows
once, as a note at the top. While the person is away, the page says it is paused instead of
checking again and again.

**Motion and access.** One short draw-in animation for charts, nothing else moves on its own, and
reduced motion is respected. Focus is always visible. Tables scroll sideways inside their panel, with a fade on the right
edge while more columns are off to the side.

**Preview.** A preview (`easy-ui preview`) says "Preview – not published" where Sign out would be
(it has no password to sign back in with), and checks for saved changes every 1.5 seconds.
Radio buttons and selects are native controls, and links in text open in a new tab, which is
announced to screen readers.
