package com.example.app.test;

import com.example.model.Entity;

/** Production code that happens to live in a package named "test". */
public class TestSupport {
    public void reset(Entity e) {
        e.setState(0);
    }
}
