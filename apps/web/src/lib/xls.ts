// .xls export: "XML Spreadsheet 2003" (SpreadsheetML), which Excel opens natively with an .xls
// extension. No dependency. Every cell is an explicit String, so a scraped title such as
// "=HYPERLINK(...)" is shown as text and never evaluated as a formula. Text is XML-escaped and
// control characters (illegal in XML) are dropped.

import { type EventItem, safeHttpUrl } from "./events";

type Item = Pick<
  EventItem,
  | "title"
  | "registration_deadline"
  | "event_start"
  | "event_end"
  | "location"
  | "mode"
  | "is_registered"
  | "is_tracked"
  | "registration_url"
>;

const COLUMNS: [header: string, width: number][] = [
  ["Title", 260],
  ["Registration closes", 130],
  ["Event starts", 130],
  ["Event ends", 130],
  ["Location", 150],
  ["Mode", 70],
  ["Registered", 70],
  ["Tracked", 70],
  ["Registration link", 260],
];

const XML_ESCAPES: Record<string, string> = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&apos;" };

/** XML-escape and strip characters XML 1.0 forbids. */
export function xmlText(s: string): string {
  return s.replace(/[\u0000-\u0008\u000B\u000C\u000E-\u001F]/g, "").replace(/[&<>"']/g, (c) => XML_ESCAPES[c]);
}

/** "2026-11-19 23:59" in `tz`, or "" when there is no date. */
export function stamp(iso: string | null, tz: string): string {
  if (!iso) return "";
  const p = Object.fromEntries(
    new Intl.DateTimeFormat("en-CA", {
      timeZone: tz,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hourCycle: "h23",
    })
      .formatToParts(new Date(iso))
      .map((x) => [x.type, x.value]),
  );
  return `${p.year}-${p.month}-${p.day} ${p.hour}:${p.minute}`;
}

const cell = (v: string, style = "") =>
  `<Cell${style ? ` ss:StyleID="${style}"` : ""}><Data ss:Type="String">${xmlText(v)}</Data></Cell>`;

/** A complete workbook with one sheet, one row per event. Times are shown in the user's timezone. */
export function buildXls(events: Item[], tz: string): string {
  const head = `<Row>${COLUMNS.map(([h]) => cell(h, "h")).join("")}</Row>`;
  const rows = events.map((e) => {
    const url = safeHttpUrl(e.registration_url) ?? "";
    return `<Row>${[
      e.title,
      stamp(e.registration_deadline, tz),
      stamp(e.event_start, tz),
      stamp(e.event_end, tz),
      e.location ?? "",
      e.mode === "offline" ? "In person" : e.mode,
      e.is_registered ? "Yes" : "No",
      e.is_tracked ? "Yes" : "No",
      url,
    ]
      .map((v) => cell(v))
      .join("")}</Row>`;
  });
  return `<?xml version="1.0" encoding="UTF-8"?>
<?mso-application progid="Excel.Sheet"?>
<Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet" xmlns:o="urn:schemas-microsoft-com:office:office" xmlns:x="urn:schemas-microsoft-com:office:excel" xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet">
<Styles><Style ss:ID="h"><Font ss:Bold="1"/><Interior ss:Color="#E6F4D0" ss:Pattern="Solid"/></Style></Styles>
<Worksheet ss:Name="HackEvents"><Table>${COLUMNS.map(([, w]) => `<Column ss:Width="${w}"/>`).join("")}${head}${rows.join("")}</Table></Worksheet>
</Workbook>
`;
}
