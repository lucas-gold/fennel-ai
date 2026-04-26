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
        for format in ["yyyy-MM-dd'T'HH:mm:ss", "yyyy-MM-dd'T'HH:mm"] {
            f.dateFormat = format
            if let d = f.date(from: s) { return d }
        }
        return nil
    }

    static func addReminder(title: String, due: Date?, notes: String?) async throws {
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
    }

    static func addEvent(title: String, start: Date, end: Date, location: String?) async throws {
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
    }
}
