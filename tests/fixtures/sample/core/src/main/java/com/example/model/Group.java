package com.example.model;

import java.util.ArrayList;
import java.util.List;

public class Group extends Entity {
    private final List<Entity> members = new ArrayList<>();

    public void add(Entity e) {
        members.add(e);
        e.setState(2);
    }

    public int getSize() {
        return members.size();
    }

    @Override
    public String uid() {
        return "G-" + members.size();
    }
}
