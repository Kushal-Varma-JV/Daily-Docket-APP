"""First Circuit tab display."""
import streamlit as st
import pandas as pd


def display_first_circuit_tab():
    st.title("🔵 First Circuit Court Calendar")
    if st.session_state.c1_parsed_cases:
        df = pd.DataFrame(st.session_state.c1_parsed_cases)

        col1, col2, col3 = st.columns(3)
        with col1:
            st.metric("Total Cases", len(df))
        with col2:
            if 'date' in df.columns:
                st.metric("Hearing Days", df['date'].dropna().nunique())
            else:
                st.metric("Hearing Days", "N/A")
        with col3:
            if 'judges_panel' in df.columns:
                st.metric("Judge Panels", df['judges_panel'].replace("", pd.NA).dropna().nunique())
            else:
                st.metric("Judge Panels", "N/A")

        st.divider()

        # Rename columns for display (user-friendly headers)
        display_rename = {
            "date":               "Date",
            "case_number":        "Case Number",
            "case_name":          "Case Name",
            "nature_of_case":     "Nature of Case",
            "court_name":         "Court Name",
            "location":           "Location",
            "judges_panel":       "Judges / Panel",
            "courtroom":          "Courtroom",
            "purpose_of_hearing": "Purpose of Hearing",
            "time":               "Time",
            "description":        "Description",
        }
        display_df = df.rename(columns=display_rename)
        st.dataframe(display_df, use_container_width=True, height=500)

        with st.expander("📄 View Raw PDF Text"):
            if st.session_state.c1_pdf_text:
                st.text_area("PDF Content", value=st.session_state.c1_pdf_text,
                             height=400, disabled=True)
    else:
        st.info("👈 Click 'Fetch First Circuit' in the sidebar to load data")