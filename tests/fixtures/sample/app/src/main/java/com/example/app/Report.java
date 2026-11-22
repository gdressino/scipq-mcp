package com.example.app;

import java.util.Date;

/** Hand-written homonyms of Lombok getters on another class. */
public class Report {
    private final Date changedAt = new Date();

    public Date getChangedAt() {
        return changedAt;
    }

    public void setState(int state) {
    }

    public long render(Report other) {
        other.setState(0);
        return other.getChangedAt().getTime();
    }
}
