# GatorTrack

GatorTrack connects GitHub Classroom assignments to Google Calendar so students never miss deadlines.

## What the tool does

1. Detects assignment acceptance

- Listens for GitHub Classroom events when a student accepts an assignment.

1. Extracts assignment details

- Captures the assignment title, due date/time, and course or repository name.

1. Creates a calendar event

- Uses the Google Calendar API to add an event with the assignment name, due date, and a repository link or description.

1. Keeps events updated

- Updates the existing calendar event if the due date changes, avoiding duplicates.

1. Maintains a mapping system

- Stores a link between each GitHub assignment and its calendar event for reliable syncing.

## In simple terms

Turns: “Student accepts assignment on GitHub and forgets it exists.”
Into: “Student accepts assignment and it instantly appears on their calendar.”

## Why this matters

Students live in their calendars, while assignments live in learning platforms. GatorTrack bridges that gap by keeping academic responsibilities visible alongside everyday commitments—reducing missed deadlines, mental load, and manual tracking.
