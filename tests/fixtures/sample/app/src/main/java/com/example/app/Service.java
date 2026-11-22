package com.example.app;

import com.example.model.Account;
import com.example.model.Entity;
import com.example.model.Group;
import com.example.model.Snapshot;
import com.example.model.Validator;

public class Service {
    private static final Entity DEFAULT;

    static {
        DEFAULT = Entity.findById(0L);
    }

    private final Account seed = new Account.Builder().number("0").build();
    private final int seedState = seed.getState();

    private final Validator notArchived = e -> {
        e.setState(3);
        return true;
    };

    public void activate(Entity e) {
        e.setState(1);
        notArchived.validate(e);
    }

    public void activateAll(Group g) {
        Validator anon = new Validator() {
            @Override
            public boolean validate(Entity e) {
                e.setState(4);
                return false;
            }
        };
        anon.validate(g);
        g.setState(5);
    }

    public String process(int n) {
        return Integer.toString(n);
    }

    public String process(String s) {
        return process(s.length());
    }

    public long age(Snapshot s) {
        return s.getChangedAt().getTime();
    }
}
