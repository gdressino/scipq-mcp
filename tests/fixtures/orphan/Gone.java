package com.example.model;

/** Compiled once, then deleted: its shard is an orphan. */
public class Gone extends Entity {
    @Override
    public String uid() {
        setState(9);
        return "gone";
    }
}
