#!/usr/bin/swift

import Foundation

// 1. Specify the duration
let totalSeconds = 10

// 2. Create a formatter to display the time nicely
let formatter = DateFormatter()
formatter.timeStyle = .medium // e.g., "2:30:45 PM"

print("Starting timer for \(totalSeconds) seconds...")

// 3. Loop from 1 to the specified duration
for i in 1...totalSeconds {
    // Get the current date and time
    let now = Date()
    
    // Format it into a string
    let timeString = formatter.string(from: now)
    
    // Print the time
    print("Second \(i): \(timeString)")
    
    // Pause execution for 1 second
    // sleep() takes a UInt32, so we cast 1.
    sleep(1)
}

print("Timer finished.")