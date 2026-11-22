package com.example.model;

/** A record: scip-javac reports it without the keyword or its components. */
public record Point(int x, int y) {
    public Point scaled(int k) {
        return new Point(x() * k, y * k);
    }
}
