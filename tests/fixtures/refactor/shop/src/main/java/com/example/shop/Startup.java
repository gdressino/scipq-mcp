package com.example.shop;

import jakarta.annotation.PostConstruct;
import java.util.ArrayList;
import java.util.List;
import java.util.function.Supplier;

/** register() only keeps a factory that runs later; warm() runs the code at startup. */
public class Startup {
    private final List<Supplier<String>> factories = new ArrayList<>();

    @PostConstruct
    public void register() {
        factories.add(() -> Pages.lazy());
    }

    @PostConstruct
    public void factory() {
        factories.add(new Supplier<String>() {
            @Override
            public String get() {
                return Pages.lazy();
            }
        });
    }

    @PostConstruct
    public void warm() {
        Pages.lazy();
    }
}
