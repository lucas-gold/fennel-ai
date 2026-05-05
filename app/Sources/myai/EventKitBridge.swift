import EventKit
import Foundation

/// The real side effects behind `set_reminder` / `add_event` (D-HOME).
///
/// The backend never touches EventKit — it only normalizes arguments into
/// absolute local times. This is the only place that writes to the user's data,
/// and it reports back so the assistant's spoken confirmation can be truthful
/// (including "I couldn't — Reminders access is off").
enum EventKitBridge {
    private static let store = EKEventStore()

    struct DeniedError: LocalizedError {
        let what: String
        var errorDescription: String? { "\(what) access is turned off in System Settings" }
    }

    /// Backend times are local wall-clock with no offset: "2026-08-25T07:00:00".
    static func parseDate(_ s: String?) -> Date? {
        guard let s, !s.isEmpty else { return nil }
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.timeZone = .current
        // Fractional seconds first: Python's isoformat() includes microseconds
        // unless explicitly stripped, and a parser that only accepts whole
        // seconds fails silently, which is worse than loudly.
        for format in ["yyyy-MM-dd'T'HH:mm:ss.SSSSSS", "yyyy-MM-dd'T'HH:mm:ss.SSS",
                       "yyyy-MM-dd'T'HH:mm:ss", "yyyy-MM-dd'T'HH:mm"] {
            f.dateFormat = format
            if let d = f.date(from: s) { return d }
        }
        return nil
    }

    /// → the EventKit identifier, so dismissing the card can remove the real
    /// entry again (and Undo can put it back).
    @discardableResult
    static func addReminder(title: String, due: Date?, notes: String?) async throws -> String {
        guard try await store.requestFullAccessToReminders() else {
            throw DeniedError(what: "Reminders")
        }
        let r = EKReminder(eventStore: store)
        r.title = title
        r.notes = notes
        r.calendar = store.defaultCalendarForNewReminders()
        if let due {
            r.dueDateComponents = Calendar.current.dateComponents(
                [.year, .month, .day, .hour, .minute], from: due)
            r.addAlarm(EKAlarm(absoluteDate: due))   // a reminder that doesn't fire isn't one
        }
        try store.save(r, commit: true)
        return r.calendarItemIdentifier
    }

    @discardableResult
    static func addEvent(title: String, start: Date, end: Date,
                         location: String?) async throws -> String {
        guard try await store.requestFullAccessToEvents() else {
            throw DeniedError(what: "Calendar")
        }
        let e = EKEvent(eventStore: store)
        e.title = title
        e.startDate = start
        e.endDate = end
        e.location = location
        e.calendar = store.defaultCalendarForNewEvents
        try store.save(e, span: .thisEvent, commit: true)
        return e.eventIdentifier
    }

    /// Read back what's already scheduled, for the `agenda` tool. Returns lines
    /// the model can simply speak, plus the count, since it only needs to say
    /// them — not reason over the structure.
    static func agenda(range: String) async throws -> (lines: [String], count: Int) {
        let cal = Calendar.current
        let now = Date()
        let start: Date, end: Date
        switch range {
        case "tomorrow":
            start = cal.startOfDay(for: cal.date(byAdding: .day, value: 1, to: now)!)
            end = cal.date(byAdding: .day, value: 1, to: start)!
        case "week":
            start = now
            end = cal.date(byAdding: .day, value: 7, to: cal.startOfDay(for: now))!
        default:
            start = now
            end = cal.date(byAdding: .day, value: 1, to: cal.startOfDay(for: now))!
        }

        let fmt = DateFormatter()
        fmt.doesRelativeDateFormatting = true
        fmt.dateStyle = (range == "today") ? .none : .medium
        fmt.timeStyle = .short

        var out: [(Date, String)] = []

        if try await store.requestFullAccessToEvents() {
            let p = store.predicateForEvents(withStart: start, end: end, calendars: nil)
            for e in store.events(matching: p) where !e.isAllDay {
                out.append((e.startDate, "\(fmt.string(from: e.startDate)): \(e.title ?? "Event")"))
            }
        }
        if try await store.requestFullAccessToReminders() {
            let p = store.predicateForIncompleteReminders(
                withDueDateStarting: start, ending: end, calendars: nil)
            let reminders: [EKReminder] = await withCheckedContinuation { cont in
                store.fetchReminders(matching: p) { cont.resume(returning: $0 ?? []) }
            }
            for r in reminders {
                let due = r.dueDateComponents.flatMap(cal.date(from:))
                let when = due.map { "\(fmt.string(from: $0)): " } ?? ""
                out.append((due ?? start, "\(when)\(r.title ?? "Reminder") (reminder)"))
            }
        }
        out.sort { $0.0 < $1.0 }
        return (out.map(\.1), out.count)
    }

    /// Deleting something already gone is success, not an error — the user may
    /// have removed it in Reminders/Calendar before dismissing the card.
    static func deleteReminder(id: String) async throws {
        guard try await store.requestFullAccessToReminders() else {
            throw DeniedError(what: "Reminders")
        }
        guard let item = store.calendarItem(withIdentifier: id) as? EKReminder else { return }
        try store.remove(item, commit: true)
    }

    static func deleteEvent(id: String) async throws {
        guard try await store.requestFullAccessToEvents() else {
            throw DeniedError(what: "Calendar")
        }
        guard let e = store.event(withIdentifier: id) else { return }
        try store.remove(e, span: .thisEvent, commit: true)
    }
}
