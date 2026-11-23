package com.example.shop;

import java.util.List;
import javax.jdo.annotations.Column;
import javax.jdo.annotations.Discriminator;
import javax.jdo.annotations.DiscriminatorStrategy;
import javax.jdo.annotations.FetchGroup;
import javax.jdo.annotations.NotPersistent;
import javax.jdo.annotations.PersistenceCapable;
import javax.jdo.annotations.Persistent;
import lombok.Getter;
import lombok.Setter;

/** A persistent order: Lombok accessors, a fetch group naming its fields. */
@PersistenceCapable(table = "ORDERS")
@Discriminator(strategy = DiscriminatorStrategy.CLASS_NAME, column = "CLASS")
@FetchGroup(name = "withLines", members = {@Persistent(name = "lines"), @Persistent(name = "state")})
@Getter
@Setter
public class Order implements HasState {
    private int state;

    @Column(name = "TOTAL_AMOUNT")
    private long total;

    @NotPersistent
    private String note;

    @Persistent(mappedBy = "order")
    private List<Line> lines;

    /** Written by hand next to Lombok's accessors. */
    public String getNote() {
        return note == null ? "" : note;
    }
}
