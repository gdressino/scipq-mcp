package com.example.model;

public class Session implements AutoCloseable {
    public Entity find(long id) {
        return Entity.findById(id);
    }

    @Override
    public void close() {
    }
}
